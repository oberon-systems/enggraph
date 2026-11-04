use std::path::Path;
use std::time::Duration;

use tokio::sync::mpsc;
use tokio::time::{interval, sleep, sleep_until, Instant, MissedTickBehavior};
use url::Url;

use crate::libs::api::{fingerprint, unix_now, Api, Claim, Signed};
use crate::libs::config::{self, Config};
use crate::libs::error::AgentError;
use crate::libs::exports::{Export, Reported};
use crate::libs::serve::Server;
use crate::libs::socket::{Backoff, Connection, Event};
use crate::libs::state::{Credential, State, StateStore};
use crate::libs::watch::Watch;

pub const CLAIM_POLL: Duration = Duration::from_secs(10);
const REFRESH_BEFORE_SECONDS: i64 = 15 * 60;
const REFRESH_RETRY: Duration = Duration::from_secs(30);
const CONFIG_POLL: Duration = Duration::from_secs(5);
const ANNOUNCE_EVERY: Duration = Duration::from_secs(5 * 60);
// An installer waits on what the last answer said, so a pending decision is asked about sooner.
const ANNOUNCE_UNSETTLED: Duration = Duration::from_secs(15);
const LASTED: Duration = Duration::from_secs(60);
// Past this, a blocked socket holds file operations back rather than filling memory.
const OUTGOING_FRAMES: usize = 64;

#[derive(Clone)]
pub struct Signer {
    pub agent_id: String,
    pub credential: Credential,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    Wait(Duration),
    Connect,
}

enum Held {
    Replaced,
    Lasted(Duration),
}

/// One task makes every signed call, in turn: no two can be in flight across a
/// swap, and after one the old generation is never signed with again.
pub struct Agent<A> {
    api: A,
    store: StateStore,
    state: State,
    stack_url: Url,
    name: String,
    labels: Vec<String>,
    exports: Vec<Export>,
    announced: bool,
    told_code: bool,
    config_broken: bool,
}

impl<A: Api> Agent<A> {
    pub fn new(config: &Config, api: A) -> Result<Self, AgentError> {
        let store = StateStore::new(&config.state_dir);
        let state = store.load()?;
        Ok(Self {
            api,
            store,
            state,
            stack_url: config.stack_url.clone(),
            name: config.name.clone(),
            labels: config.labels.clone(),
            exports: config.exports.clone(),
            announced: false,
            told_code: false,
            config_broken: false,
        })
    }

    /// One step of the loop: what to do before the next one.
    pub async fn cycle(&mut self) -> Result<Step, AgentError> {
        if self.signer().is_none() {
            return self.enroll().await;
        }
        if self.refresh_at() <= Instant::now() && !self.refresh().await? {
            return Ok(Step::Wait(Duration::ZERO));
        }
        if !self.announced && !self.announce().await? {
            return Ok(Step::Wait(Duration::ZERO));
        }
        Ok(Step::Connect)
    }

    async fn enroll(&mut self) -> Result<Step, AgentError> {
        let Some(code) = self.state.claim_code.clone() else {
            let code = self
                .api
                .register(
                    &self.name,
                    &self.labels,
                    self.state.agent_id.as_deref(),
                    &self.exports,
                )
                .await?;
            self.save(State {
                claim_code: Some(code.clone()),
                ..self.state.clone()
            })?;
            self.tell_about(&code);
            return Ok(Step::Wait(CLAIM_POLL));
        };
        if !self.told_code {
            self.tell_about(&code);
        }
        match self.api.claim(&code).await? {
            Claim::Waiting => Ok(Step::Wait(CLAIM_POLL)),
            Claim::Refused => {
                tracing::warn!("the registration was refused or expired; registering again");
                self.save(State {
                    claim_code: None,
                    ..self.state.clone()
                })?;
                self.told_code = false;
                Ok(Step::Wait(Duration::ZERO))
            }
            Claim::Granted {
                agent_id,
                credential,
            } => {
                tracing::info!(%agent_id, "claimed the credential");
                self.save(State {
                    agent_id: Some(agent_id),
                    claim_code: None,
                    credential: Some(credential),
                    exports: self.state.exports.clone(),
                })?;
                self.announced = false;
                self.told_code = false;
                Ok(Step::Wait(Duration::ZERO))
            }
        }
    }

    fn tell_about(&mut self, code: &str) {
        self.told_code = true;
        match self.stack_url.join("agents") {
            Ok(page) => tracing::info!(
                fingerprint = %fingerprint(code),
                approve_at = %page,
                "waiting for an operator"
            ),
            Err(err) => tracing::warn!(error = %err, "the stack URL has no Agents page"),
        }
    }

    /// False once the stack refused: the credential is gone and the agent registers again.
    async fn refresh(&mut self) -> Result<bool, AgentError> {
        let Some(signer) = self.signer() else {
            return Ok(false);
        };
        match self
            .api
            .refresh(&signer.agent_id, &signer.credential)
            .await?
        {
            // Saved before use: until this holds, the old generation stays good to sign with.
            Signed::Accepted(fresh) => {
                self.save(State {
                    credential: Some(fresh),
                    ..self.state.clone()
                })?;
                tracing::info!("the credential was refreshed");
                Ok(true)
            }
            Signed::Refused => {
                self.forget_credential()?;
                Ok(false)
            }
        }
    }

    /// False once the stack refused: the credential is gone and the agent registers again.
    async fn announce(&mut self) -> Result<bool, AgentError> {
        let Some(signer) = self.signer() else {
            return Ok(false);
        };
        match self
            .api
            .put_exports(&signer.agent_id, &signer.credential, &self.exports)
            .await?
        {
            Signed::Accepted(reported) => {
                self.save(State {
                    exports: reported,
                    ..self.state.clone()
                })?;
                self.announced = true;
                Ok(true)
            }
            Signed::Refused => {
                self.forget_credential()?;
                Ok(false)
            }
        }
    }

    // A refused credential sends the agent back to registration under the id it already has.
    fn forget_credential(&mut self) -> Result<(), AgentError> {
        tracing::warn!("the stack refused the credential; registering again");
        self.save(State {
            credential: None,
            ..self.state.clone()
        })?;
        self.announced = false;
        Ok(())
    }

    /// True when the exports in the config file changed. A file gone bad keeps the
    /// last good exports, and is said once rather than every few seconds.
    fn reload(&mut self, config_path: &Path) -> bool {
        match config::load(config_path) {
            Ok(config) => {
                self.config_broken = false;
                if config.exports == self.exports {
                    return false;
                }
                self.exports = config.exports;
                self.announced = false;
                tracing::info!(projects = self.exports.len(), "the projects changed");
                true
            }
            Err(err) => {
                if !self.config_broken {
                    tracing::warn!(error = %err, "keeping the projects already loaded");
                }
                self.config_broken = true;
                false
            }
        }
    }

    fn save(&mut self, state: State) -> Result<(), AgentError> {
        self.store.save(&state)?;
        self.state = state;
        Ok(())
    }

    fn announce_every(&self) -> Duration {
        let settled =
            |reported: &Reported| reported.bound_to.is_some() || reported.state == "refused";
        if self.state.exports.iter().all(settled) {
            ANNOUNCE_EVERY
        } else {
            ANNOUNCE_UNSETTLED
        }
    }

    fn signer(&self) -> Option<Signer> {
        Some(Signer {
            agent_id: self.state.agent_id.clone()?,
            credential: self.state.credential.clone()?,
        })
    }

    fn refresh_at(&self) -> Instant {
        let left = self
            .state
            .credential
            .as_ref()
            .map_or(0, |credential| credential.expires_at - unix_now());
        let wait = u64::try_from(left - REFRESH_BEFORE_SECONDS).unwrap_or(0);
        Instant::now() + Duration::from_secs(wait)
    }
}

/// Runs the agent until it is dropped: nothing past the config it started from stops it.
pub async fn supervise<A: Api>(agent: &mut Agent<A>, config_path: &Path) {
    let mut backoff = Backoff::default();
    loop {
        agent.reload(config_path);
        let pause = match agent.cycle().await {
            Ok(Step::Wait(pause)) => {
                backoff.reset();
                pause
            }
            Ok(Step::Connect) => {
                let held = hold(agent, config_path).await;
                // The next signed call says whether a lost socket was the credential.
                agent.announced = false;
                match held {
                    Held::Replaced => {
                        backoff.reset();
                        Duration::ZERO
                    }
                    Held::Lasted(lasted) => {
                        if lasted >= LASTED {
                            backoff.reset();
                        }
                        backoff.pause()
                    }
                }
            }
            Err(err) => {
                tracing::warn!(error = %err, "the stack did not answer");
                backoff.pause()
            }
        };
        sleep(pause).await;
    }
}

async fn hold<A: Api>(agent: &mut Agent<A>, config_path: &Path) -> Held {
    let Some(signer) = agent.signer() else {
        return Held::Replaced;
    };
    let opened = Connection::open(&agent.stack_url, &signer, &agent.exports).await;
    let mut connection = match opened {
        Ok(connection) => connection,
        Err(err) => {
            tracing::warn!(error = %err, "could not connect to the stack");
            return Held::Lasted(Duration::ZERO);
        }
    };
    tracing::info!("connected to the stack");
    let limits = connection.limits();
    let (out, mut outgoing) = mpsc::channel(OUTGOING_FRAMES);
    let server = Server::new(&agent.exports, limits.clone(), out.clone());
    let mut _changes = Watch::start(&agent.exports, &limits.skip_dirs, out.clone());
    let since = Instant::now();
    let mut announced_at = Instant::now();
    let mut refresh_at = agent.refresh_at();
    let mut watch = interval(CONFIG_POLL);
    watch.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            event = connection.next_event() => match event {
                Event::Heard => {}
                Event::Request(request) => server.dispatch(request),
                Event::PingDue => {
                    if let Err(err) = connection.ping().await {
                        tracing::warn!(error = %err, "lost the socket");
                        return Held::Lasted(since.elapsed());
                    }
                }
                Event::Ended(why) => {
                    tracing::warn!(why = %why, "lost the socket");
                    return Held::Lasted(since.elapsed());
                }
            },
            Some(message) = outgoing.recv() => {
                if let Err(err) = connection.send(message).await {
                    tracing::warn!(error = %err, "lost the socket");
                    return Held::Lasted(since.elapsed());
                }
            }
            _ = watch.tick() => {
                let changed = agent.reload(config_path);
                if changed {
                    server.set_exports(&agent.exports);
                    _changes = Watch::start(&agent.exports, &limits.skip_dirs, out.clone());
                }
                if changed || !agent.announced || announced_at.elapsed() >= agent.announce_every() {
                    match agent.announce().await {
                        Ok(true) => announced_at = Instant::now(),
                        Ok(false) => {
                            connection.close().await;
                            return Held::Replaced;
                        }
                        Err(err) => {
                            agent.announced = false;
                            tracing::warn!(error = %err, "could not announce the projects");
                        }
                    }
                }
            }
            () = sleep_until(refresh_at) => match agent.refresh().await {
                // This socket hangs on the old generation, so it goes before the new one signs.
                Ok(_) => {
                    connection.close().await;
                    return Held::Replaced;
                }
                Err(err) => {
                    tracing::warn!(error = %err, "could not refresh the credential");
                    refresh_at = Instant::now() + REFRESH_RETRY;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests;
