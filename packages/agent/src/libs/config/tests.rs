use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::{Path, PathBuf};

use tempfile::TempDir;

use super::{load, valid_name, Document};
use crate::libs::exports::Export;

fn write_config(dir: &TempDir, body: &str) -> PathBuf {
    let path = dir.path().join("config.toml");
    fs::write(&path, body).unwrap();
    path
}

fn body(stack_url: &str, exports: &str) -> String {
    format!(
        "stack_url = {stack_url:?}\nname = \"alpha\"\nstate_dir = \"/var/lib/alpha\"\n{exports}"
    )
}

fn export(name: &str, path: &Path) -> String {
    format!(
        "[[export]]\nname = {name:?}\npath = {:?}\n",
        path.display().to_string()
    )
}

#[test]
fn a_config_loads_with_its_paths_made_canonical() {
    let dir = tempfile::tempdir().unwrap();
    let real = dir.path().join("real");
    fs::create_dir(&real).unwrap();
    let link = dir.path().join("link");
    symlink(&real, &link).unwrap();
    let path = write_config(
        &dir,
        &body("https://stack.example.com", &export("beta", &link)),
    );

    let config = load(&path).unwrap();

    assert_eq!(config.stack_url.as_str(), "https://stack.example.com/");
    assert_eq!(config.name, "alpha");
    assert_eq!(
        config.exports,
        [Export {
            name: "beta".into(),
            path: fs::canonicalize(&real).unwrap().display().to_string(),
        }]
    );
}

#[test]
fn a_relative_export_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_config(
        &dir,
        &body(
            "https://stack.example.com",
            &export("beta", Path::new("srv/beta")),
        ),
    );

    assert!(load(&path)
        .unwrap_err()
        .to_string()
        .contains("must be absolute"));
}

#[test]
fn a_missing_export_directory_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("missing");
    let path = write_config(
        &dir,
        &body("https://stack.example.com", &export("beta", &missing)),
    );

    assert!(load(&path).is_err());
}

#[test]
fn an_export_that_is_a_file_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("file");
    fs::write(&file, "").unwrap();
    let path = write_config(
        &dir,
        &body("https://stack.example.com", &export("beta", &file)),
    );

    assert!(load(&path)
        .unwrap_err()
        .to_string()
        .contains("not a directory"));
}

#[test]
fn duplicate_export_names_are_refused() {
    let dir = tempfile::tempdir().unwrap();
    let exports = format!("{0}{0}", export("beta", dir.path()));
    let path = write_config(&dir, &body("https://stack.example.com", &exports));

    assert!(load(&path).unwrap_err().to_string().contains("twice"));
}

#[test]
fn an_unknown_field_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_config(
        &dir,
        &body("https://stack.example.com", "gamma = \"delta\"\n"),
    );

    assert!(load(&path).is_err());
}

#[test]
fn a_stack_url_the_signature_would_not_cover_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    for bad in [
        "ftp://stack.example.com",
        "https://stack.example.com/beta",
        "https://stack.example.com/?gamma=1",
        "https://user@stack.example.com",
        "stack.example.com",
    ] {
        let path = write_config(&dir, &body(bad, ""));

        assert!(load(&path).is_err(), "{bad}");
    }
}

#[test]
fn a_relative_state_dir_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_config(
        &dir,
        "stack_url = \"https://stack.example.com\"\nname = \"alpha\"\nstate_dir = \"beta\"\n",
    );

    assert!(load(&path)
        .unwrap_err()
        .to_string()
        .contains("must be absolute"));
}

#[test]
fn a_name_follows_the_rule_of_the_stack() {
    assert!(valid_name("alpha-1.beta_2", 64));
    assert!(!valid_name("", 64));
    assert!(!valid_name("-alpha", 64));
    assert!(!valid_name("al pha", 64));
    assert!(!valid_name("alpha", 4));
}

#[test]
fn only_plain_http_beyond_this_machine_is_exposed() {
    let dir = tempfile::tempdir().unwrap();
    for (url, exposed) in [
        ("https://stack.example.com", false),
        ("http://localhost:3000", false),
        ("http://127.0.0.1:3000", false),
        ("http://[::1]:3000", false),
        ("http://192.0.2.10:3000", true),
        ("http://stack.example.com", true),
    ] {
        let path = write_config(&dir, &body(url, ""));

        assert_eq!(load(&path).unwrap().exposed(), exposed, "{url}");
    }
}

#[test]
fn a_rewrite_reads_back_and_keeps_the_mode() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_config(&dir, &body("https://stack.example.com", ""));
    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    let mut document = Document::read(&path).unwrap();
    document.exports.push(Export {
        name: "beta".into(),
        path: "/srv/beta".into(),
    });

    document.write(&path).unwrap();

    assert_eq!(Document::read(&path).unwrap(), document);
    let mode = fs::metadata(&path).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o640);
    assert!(!dir.path().join("config.toml.tmp").exists());
}
