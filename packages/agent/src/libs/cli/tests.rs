use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use clap::{CommandFactory, Parser};

use super::{default_config, export, render_status, status, Cli, Command, ExportAction};
use crate::libs::api::fingerprint;
use crate::libs::config::Document;
use crate::libs::exports::{Export, Reported};
use crate::libs::state::{Credential, State};

fn document() -> Document {
    Document {
        stack_url: "https://stack.example.com".into(),
        name: "alpha".into(),
        labels: Vec::new(),
        state_dir: PathBuf::from("/var/lib/alpha"),
        exports: vec![
            Export {
                name: "beta".into(),
                path: "/srv/beta".into(),
            },
            Export {
                name: "gamma".into(),
                path: "/srv/gamma".into(),
            },
        ],
    }
}

#[test]
fn the_command_line_is_well_formed() {
    Cli::command().debug_assert();
}

#[test]
fn the_commands_parse() {
    let parsed = |args: &[&str]| Cli::try_parse_from(args).unwrap().command;

    assert_eq!(parsed(&["enggraph-agent", "run"]), Command::Run);
    assert_eq!(parsed(&["enggraph-agent", "status"]), Command::Status);
    assert_eq!(
        parsed(&["enggraph-agent", "export", "add", "beta", "/srv/beta"]),
        Command::Export {
            action: ExportAction::Add {
                name: "beta".into(),
                path: PathBuf::from("/srv/beta"),
            },
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "export", "remove", "beta"]),
        Command::Export {
            action: ExportAction::Remove {
                name: "beta".into(),
            },
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "export", "list"]),
        Command::Export {
            action: ExportAction::List,
        }
    );
}

#[test]
fn the_config_flag_reaches_every_command() {
    let cli =
        Cli::try_parse_from(["enggraph-agent", "status", "--config", "/etc/alpha.toml"]).unwrap();

    assert_eq!(cli.config, Some(PathBuf::from("/etc/alpha.toml")));
}

#[test]
fn the_default_config_follows_xdg_then_home_then_etc() {
    let dir = |text: &str| Some(OsString::from(text));

    assert_eq!(
        default_config(dir("/xdg"), dir("/home/alpha")),
        Path::new("/xdg/enggraph-agent/config.toml")
    );
    assert_eq!(
        default_config(dir(""), dir("/home/alpha")),
        Path::new("/home/alpha/.config/enggraph-agent/config.toml")
    );
    assert_eq!(
        default_config(None, None),
        Path::new("/etc/enggraph-agent/config.toml")
    );
}

#[test]
fn a_waiting_agent_shows_its_fingerprint_and_not_its_code() {
    let state = State {
        claim_code: Some("delta-claim-code".into()),
        ..State::default()
    };

    let text = render_status(&document(), &state, 0);

    assert!(text.contains("waiting for an operator"));
    assert!(text.contains(&fingerprint("delta-claim-code")));
    assert!(!text.contains("delta-claim-code"));
}

#[test]
fn an_enrolled_agent_shows_the_time_left_and_not_its_key() {
    let state = State {
        agent_id: Some("epsilon".into()),
        credential: Some(Credential {
            header: "k1:1:600".into(),
            expires_at: 600,
            key: "zeta-key".into(),
        }),
        ..State::default()
    };

    let text = render_status(&document(), &state, 0);

    assert!(text.contains("agent alpha epsilon"));
    assert!(text.contains("credential good for 10 min"));
    assert!(!text.contains("zeta-key"));
    assert!(!text.contains("k1:1:600"));
}

#[test]
fn an_agent_that_never_ran_says_so() {
    let text = render_status(&document(), &State::default(), 0);

    assert!(text.contains("agent alpha none yet"));
    assert!(text.contains("state not registered"));
}

#[test]
fn exports_show_what_the_stack_last_reported() {
    let state = State {
        exports: vec![Reported {
            name: "beta".into(),
            host_path: "/srv/beta".into(),
            state: "approved".into(),
        }],
        ..State::default()
    };

    let text = render_status(&document(), &state, 0);

    assert!(text.contains("beta /srv/beta approved\n"));
    assert!(text.contains("gamma /srv/gamma not reported\n"));
}

#[test]
fn the_commands_read_and_write_the_config_they_are_pointed_at() {
    let dir = tempfile::tempdir().unwrap();
    let state_dir = dir.path().join("state");
    let path = dir.path().join("config.toml");
    fs::write(
        &path,
        format!(
            "stack_url = \"https://stack.example.com\"\nname = \"alpha\"\nstate_dir = {:?}\n",
            state_dir.display().to_string()
        ),
    )
    .unwrap();
    let tree = dir.path().join("beta");
    fs::create_dir(&tree).unwrap();

    let added = export(
        &path,
        ExportAction::Add {
            name: "beta".into(),
            path: tree,
        },
    )
    .unwrap();
    let listed = export(&path, ExportAction::List).unwrap();
    let shown = status(&path).unwrap();
    export(
        &path,
        ExportAction::Remove {
            name: "beta".into(),
        },
    )
    .unwrap();

    assert!(added.starts_with("beta /"));
    assert!(listed.ends_with("not reported\n"));
    assert!(shown.contains("state not registered"));
    assert_eq!(export(&path, ExportAction::List).unwrap(), "");
}
