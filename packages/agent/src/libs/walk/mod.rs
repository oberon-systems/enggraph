use std::collections::BTreeSet;
use std::os::unix::fs::MetadataExt;

use md5::{Digest, Md5};
use rustix::fs::Dir;
use serde::Serialize;

use crate::libs::confine::Root;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Entry {
    pub path: String,
    pub size: u64,
    pub mtime: i64,
    pub md5: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Skipped {
    pub path: String,
    pub reason: String,
}

/// One directory, in the order `os.walk` gives a mount: its files, then its directories.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Listing {
    pub entries: Vec<Entry>,
    pub dirs: Vec<String>,
    pub skipped: Vec<Skipped>,
}

/// The text `MountTree.read` makes of these bytes: invalid UTF-8 dropped, newlines made `\n`.
pub fn decoded(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        text.push_str(chunk.valid());
    }
    if text.contains('\r') {
        return text.replace("\r\n", "\n").replace('\r', "\n");
    }
    text
}

/// The hash `indexer.compute_hash` gives that text.
pub fn text_md5(bytes: &[u8]) -> String {
    hex::encode(Md5::digest(decoded(bytes).as_bytes()))
}

fn join(top: &str, name: &str) -> String {
    if top.is_empty() {
        name.to_owned()
    } else {
        format!("{top}/{name}")
    }
}

fn names(root: &Root, top: &str) -> Result<(BTreeSet<String>, Vec<Skipped>), String> {
    let handle = root.open_dir(top).map_err(|err| err.to_string())?;
    let reader = Dir::read_from(&handle).map_err(|err| err.to_string())?;
    let mut found = BTreeSet::new();
    let mut skipped = Vec::new();
    for entry in reader {
        let entry = entry.map_err(|err| err.to_string())?;
        let raw = entry.file_name().to_bytes();
        if raw == b"." || raw == b".." {
            continue;
        }
        match std::str::from_utf8(raw) {
            Ok(name) => {
                found.insert(name.to_owned());
            }
            Err(_) => skipped.push(Skipped {
                path: join(top, &String::from_utf8_lossy(raw)),
                reason: "not UTF-8".into(),
            }),
        }
    }
    Ok((found, skipped))
}

fn hashed(root: &Root, path: &str, regular: bool, max_bytes: u64, hash: bool) -> Option<String> {
    if !hash || !regular {
        return None;
    }
    root.read(path, max_bytes)
        .ok()
        .map(|bytes| text_md5(&bytes))
}

fn entry(path: String, size: u64, mtime: i64, md5: Option<String>) -> Entry {
    Entry {
        path,
        size,
        mtime,
        md5,
    }
}

/// Lists one directory. An unreadable one is reported and answers nothing, as `os.walk` does.
///
/// A link to a directory is neither listed nor descended; a link that leaves the root is
/// listed with no hash, as a dangling link in a mount is, and never read.
pub fn list(root: &Root, top: &str, max_bytes: u64, hash: bool) -> Listing {
    let mut listing = Listing::default();
    let (found, skipped) = match names(root, top) {
        Ok(found) => found,
        Err(reason) => {
            // A top that is not there lists as empty, without a complaint.
            if root.stat(top).is_ok() {
                listing.skipped.push(Skipped {
                    path: top.to_owned(),
                    reason,
                });
            }
            return listing;
        }
    };
    listing.skipped = skipped;
    for name in found {
        let path = join(top, &name);
        let own = match root.lstat(&path) {
            Ok(own) => own,
            Err(err) => {
                listing.skipped.push(Skipped {
                    path,
                    reason: err.to_string(),
                });
                continue;
            }
        };
        if own.is_dir() {
            listing.dirs.push(path);
        } else if own.is_symlink() {
            match root.stat(&path) {
                Ok(target) if target.is_dir() => {}
                Ok(target) => {
                    let md5 = hashed(root, &path, target.is_file(), max_bytes, hash);
                    listing
                        .entries
                        .push(entry(path, target.len(), target.mtime(), md5));
                }
                Err(_) => listing
                    .entries
                    .push(entry(path, own.len(), own.mtime(), None)),
            }
        } else {
            let md5 = hashed(root, &path, own.is_file(), max_bytes, hash);
            listing
                .entries
                .push(entry(path, own.len(), own.mtime(), md5));
        }
    }
    listing
}

/// Hands over every directory under `top` in `os.walk` order, until `sink` says stop.
pub fn walk(
    root: &Root,
    top: &str,
    max_bytes: u64,
    hash: bool,
    sink: &mut dyn FnMut(Listing) -> bool,
) {
    let mut pending = vec![top.to_owned()];
    while let Some(current) = pending.pop() {
        let listing = list(root, &current, max_bytes, hash);
        pending.extend(listing.dirs.iter().rev().cloned());
        if !sink(listing) {
            return;
        }
    }
}

#[cfg(test)]
mod tests;
