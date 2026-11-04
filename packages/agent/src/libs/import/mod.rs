use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;

use rustix::termios::{tcgetattr, tcsetattr, LocalModes, OptionalActions};
use serde::Deserialize;

use crate::libs::api::HttpApi;
use crate::libs::config::{valid_name, Document};
use crate::libs::error::AgentError;
use crate::libs::exports;

const NAME_CHARS: usize = 64;
const TERMINAL: &str = "/dev/tty";
pub const TOKEN_VARIABLE: &str = "ENGGRAPH_OPERATOR_TOKEN";

/// A project the stack reads from a directory, as the import listing spells it.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Listed {
    pub name: String,
    pub root_path: String,
    pub bound_to: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    Add,
    Skip(String),
}

/// Says what becomes of one listed project on this host, touching nothing.
pub fn weigh(listed: &Listed, document: &Document) -> Outcome {
    if let Some(project) = &listed.bound_to {
        return Outcome::Skip(format!("{project} is already read through an agent"));
    }
    if let Some(held) = document
        .exports
        .iter()
        .find(|held| held.path == listed.root_path)
    {
        return Outcome::Skip(format!("already offered as {}", held.name));
    }
    if document.exports.iter().any(|held| held.name == listed.name) {
        return Outcome::Skip("another directory is offered under this name".into());
    }
    if !valid_name(&listed.name, NAME_CHARS) {
        return Outcome::Skip("its name cannot name a project of an agent".into());
    }
    match fs::canonicalize(&listed.root_path) {
        Err(_) => Outcome::Skip("no such directory on this host".into()),
        Ok(found) if !found.is_dir() => Outcome::Skip("not a directory on this host".into()),
        // A project binds on path equality alone, so a link could never bind.
        Ok(found) if found != Path::new(&listed.root_path) => {
            Outcome::Skip(format!("a link to {}", found.display()))
        }
        Ok(_) => Outcome::Add,
    }
}

/// Offers each listed project this host holds, and says what became of each.
pub fn apply(config_path: &Path, listed: &[Listed]) -> Result<String, AgentError> {
    let mut text = String::new();
    for project in listed {
        let document = Document::read(config_path)?;
        let outcome = match weigh(project, &document) {
            Outcome::Add => {
                let tree = Path::new(&project.root_path);
                exports::add(config_path, &project.name, tree)
                    .map(|_| None)
                    .unwrap_or_else(|err| Some(err.to_string()))
            }
            Outcome::Skip(why) => Some(why),
        };
        let line = match outcome {
            None => format!("added {} {}\n", project.name, project.root_path),
            Some(why) => format!("skipped {} {}: {why}\n", project.name, project.root_path),
        };
        text.push_str(&line);
    }
    Ok(text)
}

pub async fn run(
    config_path: &Path,
    api: &HttpApi,
    token: &str,
    under: &[String],
) -> Result<String, AgentError> {
    let listed = api.importable(token, under).await?;
    apply(config_path, &listed)
}

/// The variable when set; otherwise asked on the terminal, with nothing echoed.
pub fn operator_token(variable: Option<String>) -> Result<String, AgentError> {
    if let Some(token) = variable.filter(|token| !token.trim().is_empty()) {
        return Ok(token.trim().to_owned());
    }
    let mut terminal = OpenOptions::new()
        .read(true)
        .write(true)
        .open(TERMINAL)
        .map_err(|_| {
            AgentError::Config(format!(
                "no terminal to ask for an operator token: set {TOKEN_VARIABLE}"
            ))
        })?;
    let shown = tcgetattr(&terminal).map_err(std::io::Error::from)?;
    let mut hidden = shown.clone();
    hidden.local_modes.remove(LocalModes::ECHO);
    tcsetattr(&terminal, OptionalActions::Now, &hidden).map_err(std::io::Error::from)?;
    let asked = terminal
        .write_all(b"operator token (not shown): ")
        .and_then(|()| {
            let mut line = String::new();
            BufReader::new(&terminal).read_line(&mut line).map(|_| line)
        });
    tcsetattr(&terminal, OptionalActions::Now, &shown).map_err(std::io::Error::from)?;
    terminal.write_all(b"\n")?;
    let token = asked?.trim().to_owned();
    if token.is_empty() {
        return Err(AgentError::Config("no operator token given".into()));
    }
    Ok(token)
}

#[cfg(test)]
mod tests;
