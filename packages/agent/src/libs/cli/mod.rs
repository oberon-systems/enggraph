use std::ffi::OsString;
use std::fmt::Write;
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

const SYSTEM_CONFIG: &str = "/etc/enggraph-agent/config.toml";
const USER_CONFIG: &str = "enggraph-agent/config.toml";

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
    /// Enroll with the stack and hold the socket until stopped
    Run,
    /// Change or show what this agent offers
    Export {
        #[command(subcommand)]
        action: ExportAction,
    },
    /// Show the enrollment, and the exports as the stack last reported them
    Status,
}

#[derive(Debug, PartialEq, Eq, Subcommand)]
enum ExportAction {
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
        default_config(
            std::env::var_os("XDG_CONFIG_HOME"),
            std::env::var_os("HOME"),
        )
    });
    let outcome = match cli.command {
        Command::Run => run(&path).map(|()| String::new()),
        Command::Export { action } => export(&path, action),
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

fn export(path: &Path, action: ExportAction) -> Result<String, AgentError> {
    match action {
        ExportAction::Add { name, path: tree } => {
            let added = exports::add(path, &name, &tree)?;
            Ok(format!("{} {}\n", added.name, added.path))
        }
        ExportAction::Remove { name } => {
            exports::remove(path, &name)?;
            Ok(String::new())
        }
        ExportAction::List => {
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
