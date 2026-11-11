use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use clap::{CommandFactory, Parser};

use super::{
    default_config, default_state, init, located, project, render_status, status, Cli, Command,
    ProjectAction, SYSTEM_CONFIG,
};
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
        allow_plain_http: false,
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
        parsed(&[
            "enggraph-agent",
            "init",
            "--stack-url",
            "https://stack.example.com"
        ]),
        Command::Init {
            stack_url: "https://stack.example.com".into(),
            name: None,
            labels: Vec::new(),
            state_dir: None,
            allow_plain_http: false,
        }
    );
    assert_eq!(
        parsed(&[
            "enggraph-agent",
            "init",
            "--stack-url",
            "https://stack.example.com",
            "--labels",
            "delta,epsilon"
        ]),
        Command::Init {
            stack_url: "https://stack.example.com".into(),
            name: None,
            labels: vec!["delta".into(), "epsilon".into()],
            state_dir: None,
            allow_plain_http: false,
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "project", "import", "--under", "/srv"]),
        Command::Project {
            action: ProjectAction::Import {
                under: vec!["/srv".into()],
                yes: false,
            },
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "project", "add", "beta", "/srv/beta"]),
        Command::Project {
            action: ProjectAction::Add {
                name: "beta".into(),
                path: PathBuf::from("/srv/beta"),
            },
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "project", "remove", "beta"]),
        Command::Project {
            action: ProjectAction::Remove {
                name: "beta".into(),
            },
        }
    );
    assert_eq!(
        parsed(&["enggraph-agent", "project", "list"]),
        Command::Project {
            action: ProjectAction::List,
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
        Path::new("/xdg/enggraph/config.toml")
    );
    assert_eq!(
        default_config(dir(""), dir("/home/alpha")),
        Path::new("/home/alpha/.config/enggraph/config.toml")
    );
    assert_eq!(
        default_config(None, None),
        Path::new("/etc/enggraph/config.toml")
    );
}

#[test]
fn without_a_file_of_ones_own_the_machines_config_is_used() {
    let dir = tempfile::tempdir().unwrap();
    let user = dir.path().join("user.toml");
    let system = dir.path().join("system.toml");

    assert_eq!(located(user.clone(), &system), user);
    fs::write(&system, "").unwrap();
    assert_eq!(located(user.clone(), &system), system);
    fs::write(&user, "").unwrap();
    assert_eq!(located(user.clone(), &system), user);
}

#[test]
fn the_default_state_follows_the_config() {
    let dir = |text: &str| Some(OsString::from(text));
    let user = Path::new("/home/alpha/.config/enggraph/config.toml");

    assert_eq!(
        default_state(user, dir("/xdg"), dir("/home/alpha")),
        Path::new("/xdg/enggraph")
    );
    assert_eq!(
        default_state(user, dir(""), dir("/home/alpha")),
        Path::new("/home/alpha/.local/state/enggraph")
    );
    assert_eq!(
        default_state(user, None, None),
        Path::new("/var/lib/enggraph")
    );
    assert_eq!(
        default_state(Path::new(SYSTEM_CONFIG), dir("/xdg"), dir("/home/alpha")),
        Path::new("/var/lib/enggraph")
    );
}

#[test]
fn init_writes_a_config_the_other_commands_use() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("nested/config.toml");
    let tree = dir.path().join("beta");
    fs::create_dir(&tree).unwrap();

    let shown = init(
        &path,
        "https://stack.example.com".into(),
        Some("alpha".into()),
        Vec::new(),
        Some(dir.path().join("state")),
        false,
    )
    .unwrap();
    project(
        &path,
        ProjectAction::Add {
            name: "beta".into(),
            path: tree,
        },
    )
    .unwrap();

    assert!(shown.contains("agent alpha\n"));
    assert!(status(&path).unwrap().contains("state not registered"));
    assert!(project(&path, ProjectAction::List)
        .unwrap()
        .starts_with("beta /"));
}

#[test]
fn init_takes_the_host_name_when_none_is_given() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");

    init(
        &path,
        "https://stack.example.com".into(),
        None,
        Vec::new(),
        Some(dir.path().join("state")),
        false,
    )
    .unwrap();

    assert!(!Document::read(&path).unwrap().name.is_empty());
}

#[test]
fn init_never_replaces_a_config() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");
    fs::write(&path, "kept").unwrap();

    let refused = init(
        &path,
        "https://stack.example.com".into(),
        Some("alpha".into()),
        Vec::new(),
        Some(dir.path().join("state")),
        false,
    );

    assert!(refused.is_err());
    assert_eq!(fs::read_to_string(&path).unwrap(), "kept");
}

#[test]
fn init_refuses_what_a_run_would_refuse() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");

    let refused = init(
        &path,
        "https://stack.example.com/path".into(),
        Some("alpha".into()),
        Vec::new(),
        Some(dir.path().join("state")),
        false,
    );

    assert!(refused.is_err());
    assert!(!path.exists());
}

#[test]
fn a_command_without_a_config_says_how_to_write_one() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");

    let refused = project(&path, ProjectAction::List).unwrap_err().to_string();

    assert!(refused.contains("enggraph-agent init"), "{refused}");
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
            bound_to: None,
        }],
        ..State::default()
    };

    let text = render_status(&document(), &state, 0);

    assert!(text.contains("beta /srv/beta approved\n"));
    assert!(text.contains("gamma /srv/gamma not reported\n"));
}

#[test]
fn init_keeps_the_labels_it_is_given() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("config.toml");

    init(
        &path,
        "https://stack.example.com".into(),
        Some("alpha".into()),
        vec!["delta".into()],
        Some(dir.path().join("state")),
        false,
    )
    .unwrap();

    assert_eq!(Document::read(&path).unwrap().labels, ["delta"]);
}

#[test]
fn a_bound_project_says_what_it_is_bound_to() {
    let state = State {
        exports: vec![Reported {
            name: "beta".into(),
            host_path: "/srv/beta".into(),
            state: "approved".into(),
            bound_to: Some("delta".into()),
        }],
        ..State::default()
    };

    let text = render_status(&document(), &state, 0);

    assert!(text.contains("beta /srv/beta approved bound to delta\n"));
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

    let added = project(
        &path,
        ProjectAction::Add {
            name: "beta".into(),
            path: tree,
        },
    )
    .unwrap();
    let listed = project(&path, ProjectAction::List).unwrap();
    let shown = status(&path).unwrap();
    project(
        &path,
        ProjectAction::Remove {
            name: "beta".into(),
        },
    )
    .unwrap();

    assert!(added.starts_with("beta /"));
    assert!(listed.ends_with("not reported\n"));
    assert!(shown.contains("state not registered"));
    assert_eq!(project(&path, ProjectAction::List).unwrap(), "");
}
