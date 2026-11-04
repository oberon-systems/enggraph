use std::collections::HashSet;
use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::libs::config::{valid_name, Document};
use crate::libs::error::AgentError;

const NAME_CHARS: usize = 64;
const MAX_PATH: usize = 4096;
const MAX_EXPORTS: usize = 256;

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
pub fn check(entry: &Export) -> Result<Export, AgentError> {
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

pub fn check_all(entries: &[Export]) -> Result<Vec<Export>, AgentError> {
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
            check(entry)
        })
        .collect()
}

/// Writes a new export into the config file; a running agent picks it up from there.
pub fn add(config_path: &Path, name: &str, path: &Path) -> Result<Export, AgentError> {
    let mut document = Document::read(config_path)?;
    let path = path
        .to_str()
        .ok_or_else(|| AgentError::Config(format!("{} is not UTF-8", path.display())))?;
    let export = check(&Export {
        name: name.to_owned(),
        path: path.to_owned(),
    })?;
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
