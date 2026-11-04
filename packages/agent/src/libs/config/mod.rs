use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{fchown, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use url::{Host, Url};

use crate::libs::error::AgentError;
use crate::libs::exports::{self, Export};

const NAME_CHARS: usize = 64;
const LABEL_CHARS: usize = 32;
const MAX_LABELS: usize = 16;
const NEW_FILE_MODE: u32 = 0o644;

/// The config file as written; `load` turns it into a `Config` or says why not.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub stack_url: String,
    pub name: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub labels: Vec<String>,
    pub state_dir: PathBuf,
    #[serde(default, rename = "project", skip_serializing_if = "Vec::is_empty")]
    pub exports: Vec<Export>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub stack_url: Url,
    pub name: String,
    pub labels: Vec<String>,
    pub state_dir: PathBuf,
    pub exports: Vec<Export>,
}

pub fn load(path: &Path) -> Result<Config, AgentError> {
    Document::read(path)?.validate()
}

impl Document {
    pub fn read(path: &Path) -> Result<Self, AgentError> {
        let raw = fs::read_to_string(path).map_err(|err| {
            let hint = if err.kind() == std::io::ErrorKind::NotFound {
                "; write it with `enggraph-agent init --stack-url <url>`"
            } else {
                ""
            };
            AgentError::Config(format!("{}: {err}{hint}", path.display()))
        })?;
        toml::from_str(&raw).map_err(|err| AgentError::Config(format!("{}: {err}", path.display())))
    }

    /// Writes a new file; a config that exists is never replaced by this.
    pub fn create(&self, path: &Path) -> Result<(), AgentError> {
        let refuse = |err: String| AgentError::Config(format!("{}: {err}", path.display()));
        let body = toml::to_string_pretty(self).map_err(|err| refuse(err.to_string()))?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|err| refuse(err.to_string()))?;
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(NEW_FILE_MODE)
            .open(path)
            .map_err(|err| refuse(err.to_string()))?;
        file.write_all(body.as_bytes())?;
        file.sync_all()?;
        Ok(())
    }

    /// Replaces the file in one step, so a running agent never reads half of it.
    pub fn write(&self, path: &Path) -> Result<(), AgentError> {
        let body = toml::to_string_pretty(self)
            .map_err(|err| AgentError::Config(format!("{}: {err}", path.display())))?;
        let target = fs::canonicalize(path)?;
        let owner = fs::metadata(&target)?;
        let mode = owner.permissions().mode() & 0o777;
        let mut tmp = target.clone().into_os_string();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        match fs::remove_file(&tmp) {
            Err(err) if err.kind() != std::io::ErrorKind::NotFound => return Err(err.into()),
            _ => {}
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&tmp)?;
        // Through sudo, a rename would otherwise leave a system unit a file it cannot read.
        fchown(&file, Some(owner.uid()), Some(owner.gid()))?;
        file.set_permissions(fs::Permissions::from_mode(mode))?;
        file.write_all(body.as_bytes())?;
        file.sync_all()?;
        fs::rename(&tmp, &target)?;
        Ok(())
    }

    pub fn validate(self) -> Result<Config, AgentError> {
        let stack_url = stack_url(&self.stack_url)?;
        if !valid_name(&self.name, NAME_CHARS) {
            return Err(AgentError::Config(format!(
                "{:?} is not an agent name",
                self.name
            )));
        }
        if self.labels.len() > MAX_LABELS {
            return Err(AgentError::Config(format!(
                "at most {MAX_LABELS} labels are allowed"
            )));
        }
        if let Some(label) = self
            .labels
            .iter()
            .find(|label| !valid_name(label, LABEL_CHARS))
        {
            return Err(AgentError::Config(format!("{label:?} is not a label")));
        }
        if !self.state_dir.is_absolute() {
            return Err(AgentError::Config(format!(
                "state_dir {} must be absolute",
                self.state_dir.display()
            )));
        }
        Ok(Config {
            stack_url,
            exports: exports::check_all(&self.exports)?,
            name: self.name,
            labels: self.labels,
            state_dir: self.state_dir,
        })
    }
}

impl Config {
    /// Plain http beyond this machine shows the credential's key to the network once.
    pub fn exposed(&self) -> bool {
        self.stack_url.scheme() == "http"
            && match self.stack_url.host() {
                Some(Host::Domain(name)) => name != "localhost",
                Some(Host::Ipv4(address)) => !address.is_loopback(),
                Some(Host::Ipv6(address)) => !address.is_loopback(),
                None => true,
            }
    }
}

/// The signature covers the path the gateway sees, so the stack sits at the root of its URL.
fn stack_url(text: &str) -> Result<Url, AgentError> {
    let refuse = |why: &str| AgentError::Config(format!("stack_url {text:?} {why}"));
    let url = Url::parse(text).map_err(|err| refuse(&err.to_string()))?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err(refuse("must be http or https"));
    }
    if url.host().is_none() {
        return Err(refuse("must name a host"));
    }
    if url.path() != "/" {
        return Err(refuse("must have no path"));
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err(refuse("must have no query"));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(refuse("must carry no user"));
    }
    Ok(url)
}

/// The same rule the stack holds a name to.
pub fn valid_name(name: &str, longest: usize) -> bool {
    let mut chars = name.chars();
    name.len() <= longest
        && chars
            .next()
            .is_some_and(|first| first.is_ascii_alphanumeric())
        && chars.all(|next| next.is_ascii_alphanumeric() || matches!(next, '.' | '_' | '-'))
}

#[cfg(test)]
mod tests;
