use std::fmt;
use std::fs::{self, DirBuilder, File, OpenOptions, TryLockError};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::libs::error::AgentError;
use crate::libs::exports::Reported;

const PRIVATE_MODE: u32 = 0o600;
const PRIVATE_DIR_MODE: u32 = 0o700;
const MAX_STATE_FILE: u64 = 1024 * 1024;

/// What the agent signs with: `header` goes verbatim into `X-Credential`.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Credential {
    #[serde(alias = "credential")]
    pub header: String,
    pub expires_at: i64,
    pub key: String,
}

impl fmt::Debug for Credential {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Credential")
            .field("header", &self.header)
            .field("expires_at", &self.expires_at)
            .field("key", &"<redacted>")
            .finish()
    }
}

/// Everything the agent keeps across restarts; the id survives a new registration.
#[derive(Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct State {
    pub agent_id: Option<String>,
    pub claim_code: Option<String>,
    pub credential: Option<Credential>,
    #[serde(default)]
    pub exports: Vec<Reported>,
}

impl fmt::Debug for State {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("State")
            .field("agent_id", &self.agent_id)
            .field(
                "claim_code",
                &self.claim_code.as_ref().map(|_| "<redacted>"),
            )
            .field("credential", &self.credential)
            .field("exports", &self.exports)
            .finish()
    }
}

#[derive(Debug, Clone)]
pub struct StateStore {
    dir: PathBuf,
    path: PathBuf,
}

impl StateStore {
    pub fn new(state_dir: &Path) -> Self {
        Self {
            dir: state_dir.to_path_buf(),
            path: state_dir.join("state.json"),
        }
    }

    pub fn prepare(&self) -> Result<(), AgentError> {
        DirBuilder::new()
            .recursive(true)
            .mode(PRIVATE_DIR_MODE)
            .create(&self.dir)?;
        Ok(())
    }

    /// Held for as long as the agent runs: a second one signing from the same
    /// state could use a superseded generation, which disables the agent.
    pub fn lock(&self) -> Result<File, AgentError> {
        let path = self.dir.join("lock");
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .mode(PRIVATE_MODE)
            .open(&path)?;
        match file.try_lock() {
            Ok(()) => Ok(file),
            Err(TryLockError::WouldBlock) => Err(AgentError::State(format!(
                "another agent runs from {}",
                self.dir.display()
            ))),
            Err(TryLockError::Error(err)) => Err(err.into()),
        }
    }

    pub fn load(&self) -> Result<State, AgentError> {
        let raw = match read_private(&self.path) {
            Ok(raw) => raw,
            Err(AgentError::Io(err)) if err.kind() == ErrorKind::NotFound => {
                return Ok(State::default())
            }
            Err(err) => return Err(err),
        };
        serde_json::from_str(&raw)
            .map_err(|_| AgentError::State(format!("{} is malformed", self.path.display())))
    }

    pub fn save(&self, state: &State) -> Result<(), AgentError> {
        let body = serde_json::to_vec(state).map_err(|err| AgentError::State(err.to_string()))?;
        let tmp = self.path.with_extension("json.tmp");
        match fs::remove_file(&tmp) {
            Err(err) if err.kind() != ErrorKind::NotFound => return Err(err.into()),
            _ => {}
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(PRIVATE_MODE)
            .open(&tmp)?;
        file.write_all(&body)?;
        file.sync_all()?;
        fs::rename(&tmp, &self.path)?;
        // A crash must not bring back an older generation: signing with it disables the agent.
        File::open(&self.dir)?.sync_all()?;
        Ok(())
    }
}

fn read_private(path: &Path) -> Result<String, AgentError> {
    let reject = |why: &str| AgentError::State(format!("{} {why}", path.display()));
    if fs::symlink_metadata(path)?.file_type().is_symlink() {
        return Err(reject("must not be a symlink"));
    }
    let file = File::open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() {
        return Err(reject("must be a regular file"));
    }
    if meta.permissions().mode() & 0o077 != 0 {
        return Err(reject("must be private to its owner"));
    }
    if meta.len() > MAX_STATE_FILE {
        return Err(reject("is too large"));
    }
    let mut raw = String::new();
    file.take(MAX_STATE_FILE).read_to_string(&mut raw)?;
    Ok(raw)
}

#[cfg(test)]
mod tests;
