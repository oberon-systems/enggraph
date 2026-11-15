use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::libs::config::{valid_name, Document};
use crate::libs::error::AgentError;

const NAME_CHARS: usize = 64;
const MAX_PATH: usize = 4096;
const MAX_EXPORTS: usize = 256;
const PASSWD: &str = "/etc/passwd";
const FIRST_PERSON_UID: u32 = 1000;
const NOBODY_UID: u32 = 65534;

/// What is never offered: the root, a home, and whatever holds the agent's own files.
#[derive(Debug, Default, Clone)]
pub struct Kept {
    homes: Vec<PathBuf>,
    state: Option<PathBuf>,
    config: Option<PathBuf>,
}

impl Kept {
    pub fn new(state_dir: &Path, config_path: Option<&Path>, homes: &[PathBuf]) -> Self {
        let beside = |path: &Path| match path.parent() {
            Some(parent) if parent.as_os_str().is_empty() => settled(Path::new(".")),
            Some(parent) => settled(parent),
            None => settled(path),
        };
        Self {
            homes: homes.iter().map(|home| settled(home)).collect(),
            state: Some(settled(state_dir)),
            config: config_path.map(beside),
        }
    }

    /// With the homes this host's passwd file and $HOME name.
    pub fn of_host(state_dir: &Path, config_path: Option<&Path>) -> Self {
        Self::new(state_dir, config_path, &host_homes())
    }

    /// Says why a canonical directory is never offered, if it is not.
    pub fn refusal(&self, found: &Path) -> Option<String> {
        if found.parent().is_none() || self.homes.iter().any(|home| home.starts_with(found)) {
            return Some("the root of the host, a home or what holds one is never offered".into());
        }
        // A tree holding the credential would hand the signing key to the stack.
        let holds =
            |kept: &Option<PathBuf>| kept.as_deref().is_some_and(|dir| dir.starts_with(found));
        let inside = self
            .state
            .as_deref()
            .is_some_and(|state| found.starts_with(state));
        if holds(&self.state) || holds(&self.config) || inside {
            return Some("it holds the agent's own state or config".into());
        }
        None
    }
}

/// The real path of a directory that may not exist yet: its nearest real ancestor, then the rest.
pub fn settled(path: &Path) -> PathBuf {
    let mut head = path.to_path_buf();
    let mut rest: Vec<OsString> = Vec::new();
    loop {
        if let Ok(mut whole) = fs::canonicalize(&head) {
            for part in rest.iter().rev() {
                if part == ".." {
                    whole.pop();
                } else if part != "." {
                    whole.push(part);
                }
            }
            return whole;
        }
        match head.components().next_back() {
            Some(last @ (Component::Normal(_) | Component::ParentDir | Component::CurDir)) => {
                rest.push(last.as_os_str().to_owned());
            }
            _ => return path.to_owned(),
        }
        if !head.pop() {
            return path.to_owned();
        }
    }
}

/// The homes of root and of people in a passwd file, which lists services among them.
pub fn passwd_homes(text: &str) -> Vec<PathBuf> {
    text.lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split(':').collect();
            let uid: u32 = fields.get(2)?.parse().ok()?;
            let home = fields.get(5)?;
            let person = uid == 0 || (uid >= FIRST_PERSON_UID && uid != NOBODY_UID);
            person.then(|| PathBuf::from(home))
        })
        .collect()
}

pub fn host_homes() -> Vec<PathBuf> {
    let mut homes: Vec<PathBuf> = std::env::var_os("HOME")
        .map(PathBuf::from)
        .into_iter()
        .collect();
    if let Ok(text) = fs::read_to_string(PASSWD) {
        homes.extend(passwd_homes(&text));
    }
    homes.retain(|home| home.is_absolute());
    homes
}

/// A directory this agent offers, spelled the way the stack reads it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Export {
    pub name: String,
    pub path: String,
}

/// Where the stack last said an export stands.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reported {
    pub name: String,
    pub host_path: String,
    pub state: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bound_to: Option<String>,
}

/// Holds one export to a name and an absolute directory, its path made canonical.
pub fn check(entry: &Export, kept: &Kept) -> Result<Export, AgentError> {
    let refuse = |why: String| AgentError::Config(format!("project {}: {why}", entry.name));
    if !valid_name(&entry.name, NAME_CHARS) {
        return Err(refuse("not a project name".into()));
    }
    let path = Path::new(&entry.path);
    if !path.is_absolute() {
        return Err(refuse(format!("{} must be absolute", entry.path)));
    }
    let canonical =
        fs::canonicalize(path).map_err(|err| refuse(format!("{}: {err}", entry.path)))?;
    if !canonical.is_dir() {
        return Err(refuse(format!("{} is not a directory", entry.path)));
    }
    if let Some(why) = kept.refusal(&canonical) {
        return Err(refuse(format!("{}: {why}", entry.path)));
    }
    let text = canonical
        .to_str()
        .ok_or_else(|| refuse(format!("{} is not UTF-8", entry.path)))?;
    if text.len() > MAX_PATH {
        return Err(refuse("the path is too long".into()));
    }
    Ok(Export {
        name: entry.name.clone(),
        path: text.to_owned(),
    })
}

pub fn check_all(entries: &[Export], kept: &Kept) -> Result<Vec<Export>, AgentError> {
    if entries.len() > MAX_EXPORTS {
        return Err(AgentError::Config(format!(
            "at most {MAX_EXPORTS} projects are allowed"
        )));
    }
    let mut names = HashSet::new();
    entries
        .iter()
        .map(|entry| {
            if !names.insert(entry.name.as_str()) {
                return Err(AgentError::Config(format!(
                    "project {} is named twice",
                    entry.name
                )));
            }
            check(entry, kept)
        })
        .collect()
}

/// Writes a new export into the config file; a running agent picks it up from there.
pub fn add(config_path: &Path, name: &str, path: &Path) -> Result<Export, AgentError> {
    let mut document = Document::read(config_path)?;
    let path = path
        .to_str()
        .ok_or_else(|| AgentError::Config(format!("{} is not UTF-8", path.display())))?;
    let kept = Kept::of_host(&document.state_dir, Some(config_path));
    let entry = Export {
        name: name.to_owned(),
        path: path.to_owned(),
    };
    let export = check(&entry, &kept)?;
    if document.exports.iter().any(|entry| entry.name == name) {
        return Err(AgentError::Config(format!("project {name} is named twice")));
    }
    if document.exports.len() >= MAX_EXPORTS {
        return Err(AgentError::Config(format!(
            "at most {MAX_EXPORTS} projects are allowed"
        )));
    }
    document.exports.push(export.clone());
    document.write(config_path)?;
    Ok(export)
}

/// Takes an export out of the config file, whether or not its directory still exists.
pub fn remove(config_path: &Path, name: &str) -> Result<(), AgentError> {
    let mut document = Document::read(config_path)?;
    let before = document.exports.len();
    document.exports.retain(|entry| entry.name != name);
    if document.exports.len() == before {
        return Err(AgentError::Config(format!("no project {name}")));
    }
    document.write(config_path)
}

#[cfg(test)]
mod tests;
