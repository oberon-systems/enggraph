use std::ffi::OsString;
use std::fmt::Write;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use tokio::signal::unix::{signal, SignalKind};
use tracing_subscriber::EnvFilter;

use crate::libs::api::{fingerprint, unix_now, HttpApi};
use crate::libs::config::{self, Config, Document};
use crate::libs::enroll::{supervise, Agent};
use crate::libs::error::AgentError;
use crate::libs::exports;
use crate::libs::state::{State, StateStore};

const SYSTEM_CONFIG: &str = "/etc/enggraph/config.toml";
const USER_CONFIG: &str = "enggraph/config.toml";
const SYSTEM_STATE: &str = "/var/lib/enggraph";
const USER_STATE: &str = "enggraph";
const HOSTNAME: &str = "/proc/sys/kernel/hostname";

#[derive(Debug, Parser)]
#[command(name = "enggraph-agent", version, about)]
struct Cli {
    /// The config file; by default under $XDG_CONFIG_HOME, ~/.config or /etc
    #[arg(long, global = true, env = "ENGGRAPH_AGENT_CONFIG")]
    config: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, PartialEq, Eq, Subcommand)]
enum Command {
    /// Write the config file; directories to offer come after, with `project add`
    Init {
        /// The gateway, with no path
        #[arg(long)]
        stack_url: String,
        /// What the operator sees; the host name by default
        #[arg(long)]
        name: Option<String>,
        /// Where the credential is kept; under $XDG_STATE_HOME, ~/.local/state or /var/lib
        #[arg(long)]
        state_dir: Option<PathBuf>,
    },
    /// Enroll with the stack and hold the socket until stopped
    Run,
    /// Change or show what this agent offers
    Project {
        #[command(subcommand)]
        action: ProjectAction,
    },
    /// Show the enrollment, and the projects as the stack last reported them
    Status,
}

#[derive(Debug, PartialEq, Eq, Subcommand)]
enum ProjectAction {
    /// Offer a directory; a running agent announces it within seconds
    Add { name: String, path: PathBuf },
    /// Stop offering a directory
    Remove { name: String },
    /// Show what is offered
    List,
}

pub fn main() -> ExitCode {
    let cli = Cli::parse();
    let path = cli.config.unwrap_or_else(|| {
        let user = default_config(
            std::env::var_os("XDG_CONFIG_HOME"),
            std::env::var_os("HOME"),
        );
        located(user, Path::new(SYSTEM_CONFIG))
    });
    let outcome = match cli.command {
        Command::Init {
            stack_url,
            name,
            state_dir,
        } => init(&path, stack_url, name, state_dir),
        Command::Run => run(&path).map(|()| String::new()),
        Command::Project { action } => project(&path, action),
        Command::Status => status(&path),
    };
    match outcome {
        Ok(text) => {
            print!("{text}");
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("{err}");
            ExitCode::FAILURE
        }
    }
}

fn default_config(xdg: Option<OsString>, home: Option<OsString>) -> PathBuf {
    let usable = |dir: &OsString| !dir.is_empty();
    if let Some(dir) = xdg.filter(usable) {
        return PathBuf::from(dir).join(USER_CONFIG);
    }
    if let Some(dir) = home.filter(usable) {
        return PathBuf::from(dir).join(".config").join(USER_CONFIG);
    }
    PathBuf::from(SYSTEM_CONFIG)
}

/// Whoever has no file of their own means the machine's agent, the one a system unit runs.
fn located(user: PathBuf, system: &Path) -> PathBuf {
    if !user.exists() && system.exists() {
        return system.to_owned();
    }
    user
}

fn default_state(config: &Path, xdg: Option<OsString>, home: Option<OsString>) -> PathBuf {
    let usable = |dir: &OsString| !dir.is_empty();
    if config == Path::new(SYSTEM_CONFIG) {
        return PathBuf::from(SYSTEM_STATE);
    }
    if let Some(dir) = xdg.filter(usable) {
        return PathBuf::from(dir).join(USER_STATE);
    }
    if let Some(dir) = home.filter(usable) {
        return PathBuf::from(dir)
            .join(".local")
            .join("state")
            .join(USER_STATE);
    }
    PathBuf::from(SYSTEM_STATE)
}

fn init(
    path: &Path,
    stack_url: String,
    name: Option<String>,
    state_dir: Option<PathBuf>,
) -> Result<String, AgentError> {
    let name = match name {
        Some(name) => name,
        None => fs::read_to_string(HOSTNAME)?.trim().to_owned(),
    };
    let state_dir = state_dir.unwrap_or_else(|| {
        default_state(
            path,
            std::env::var_os("XDG_STATE_HOME"),
            std::env::var_os("HOME"),
        )
    });
    let document = Document {
        stack_url,
        name,
        labels: Vec::new(),
        state_dir,
        exports: Vec::new(),
    };
    document.clone().validate()?;
    document.create(path)?;
    Ok(format!(
        "{}\nagent {}\nstate {}\n",
        path.display(),
        document.name,
        document.state_dir.display()
    ))
}

fn run(path: &Path) -> Result<(), AgentError> {
    let config = config::load(path)?;
    // No time and no colour: journald adds the first, and would store the second.
    tracing_subscriber::fmt()
        .with_ansi(false)
        .without_time()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .init();
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(serve(path, config))
}

async fn serve(path: &Path, config: Config) -> Result<(), AgentError> {
    let store = StateStore::new(&config.state_dir);
    store.prepare()?;
    let _lock = store.lock()?;
    if config.exposed() {
        tracing::warn!(
            stack_url = %config.stack_url,
            "plain http shows the credential's key to the network; use https"
        );
    }
    let api = HttpApi::new(config.stack_url.clone())?;
    let mut agent = Agent::new(&config, api)?;
    let mut terminate = signal(SignalKind::terminate())?;
    tracing::info!(
        name = %config.name,
        version = env!("CARGO_PKG_VERSION"),
        "enggraph-agent started"
    );
    tokio::select! {
        () = supervise(&mut agent, path) => {}
        _ = terminate.recv() => {}
        _ = tokio::signal::ctrl_c() => {}
    }
    tracing::info!("enggraph-agent stopped by signal");
    Ok(())
}

fn project(path: &Path, action: ProjectAction) -> Result<String, AgentError> {
    match action {
        ProjectAction::Add { name, path: tree } => {
            let added = exports::add(path, &name, &tree)?;
            Ok(format!("{} {}\n", added.name, added.path))
        }
        ProjectAction::Remove { name } => {
            exports::remove(path, &name)?;
            Ok(String::new())
        }
        ProjectAction::List => {
            let document = Document::read(path)?;
            let state = StateStore::new(&document.state_dir).load()?;
            Ok(export_lines(&document, &state))
        }
    }
}

fn status(path: &Path) -> Result<String, AgentError> {
    let document = Document::read(path)?;
    let state = StateStore::new(&document.state_dir).load()?;
    Ok(render_status(&document, &state, unix_now()))
}

fn export_lines(document: &Document, state: &State) -> String {
    let mut text = String::new();
    for export in &document.exports {
        let standing = state
            .exports
            .iter()
            .find(|reported| reported.name == export.name)
            .map_or("not reported", |reported| reported.state.as_str());
        let _ = writeln!(text, "{} {} {standing}", export.name, export.path);
    }
    text
}

/// Says the fingerprint of a waiting claim and never the code or the key.
fn render_status(document: &Document, state: &State, now: i64) -> String {
    let mut text = String::new();
    let agent_id = state.agent_id.as_deref().unwrap_or("none yet");
    let _ = writeln!(text, "agent {} {agent_id}", document.name);
    let _ = writeln!(text, "stack {}", document.stack_url);
    match (&state.credential, &state.claim_code) {
        (Some(credential), _) => {
            let left = (credential.expires_at - now).max(0) / 60;
            let _ = writeln!(text, "state enrolled, credential good for {left} min");
        }
        (None, Some(code)) => {
            let _ = writeln!(
                text,
                "state waiting for an operator, fingerprint {}",
                fingerprint(code)
            );
        }
        (None, None) => {
            let _ = writeln!(text, "state not registered");
        }
    }
    text.push_str(&export_lines(document, state));
    text
}

#[cfg(test)]
mod tests;
