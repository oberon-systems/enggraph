use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

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

/// What an import may offer, whatever the stack lists.
#[derive(Debug, Default)]
pub struct Limits {
    under: Vec<PathBuf>,
    kept: Vec<PathBuf>,
    home: Option<PathBuf>,
}

impl Limits {
    pub fn new(
        under: &[String],
        document: &Document,
        config_path: &Path,
        home: Option<PathBuf>,
    ) -> Self {
        let real = |path: &Path| fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        let mut kept = vec![real(&document.state_dir)];
        kept.extend(config_path.parent().map(real));
        Self {
            under: under.iter().map(|dir| real(Path::new(dir))).collect(),
            kept,
            home: home.map(|dir| real(&dir)),
        }
    }

    /// Says why a canonical directory is not this import's to offer, if it is not.
    fn refusal(&self, found: &Path) -> Option<String> {
        if found.parent().is_none() || self.home.as_deref() == Some(found) {
            return Some("the root of the host or of a home is never offered".into());
        }
        // A tree holding the credential would hand the signing key to the stack.
        if self.kept.iter().any(|kept| kept.starts_with(found))
            || self
                .kept
                .first()
                .is_some_and(|state| found.starts_with(state))
        {
            return Some("it holds the agent's own state or config".into());
        }
        if !self.under.is_empty() && !self.under.iter().any(|dir| found.starts_with(dir)) {
            return Some("outside the directories given with --under".into());
        }
        None
    }
}

/// Says what becomes of one listed project on this host, touching nothing.
pub fn weigh(listed: &Listed, document: &Document, limits: &Limits) -> Outcome {
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
        Ok(found) => limits.refusal(&found).map_or(Outcome::Add, Outcome::Skip),
    }
}

/// Offers each listed project this host holds, and says what became of each.
pub fn apply(config_path: &Path, listed: &[Listed], limits: &Limits) -> Result<String, AgentError> {
    let mut text = String::new();
    for project in listed {
        let document = Document::read(config_path)?;
        let outcome = match weigh(project, &document, limits) {
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
    home: Option<PathBuf>,
) -> Result<String, AgentError> {
    let limits = Limits::new(under, &Document::read(config_path)?, config_path, home);
    let listed = api.importable(token, under).await?;
    apply(config_path, &listed, &limits)
}

/// Asks before an import with no --under takes every directory the stack lists.
pub fn confirm_everything() -> Result<(), AgentError> {
    let refuse = || {
        AgentError::Config(
            "nothing imported: give --under DIR, or --yes to take every directory the \
             stack lists"
                .into(),
        )
    };
    let mut terminal = OpenOptions::new()
        .read(true)
        .write(true)
        .open(TERMINAL)
        .map_err(|_| refuse())?;
    terminal.write_all(b"no --under given: offer every directory the stack lists? [y/N] ")?;
    let mut line = String::new();
    BufReader::new(&terminal).read_line(&mut line)?;
    if matches!(line.trim(), "y" | "Y" | "yes") {
        return Ok(());
    }
    Err(refuse())
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
