use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc as channel, Arc};
use std::thread;
use std::time::{Duration, Instant};

use notify::{recommended_watcher, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use crate::libs::confine::{is_denied, Root};
use crate::libs::exports::Export;
use crate::libs::protocol::{changed, resync};
use crate::libs::walk;

const TICK: Duration = Duration::from_millis(200);
const DEBOUNCE: Duration = Duration::from_secs(1);
// Past this a list of paths says less than asking for a full pass does.
const MAX_PATHS: usize = 1000;
// Listings here hash nothing, so no file is read and no size limit applies.
const NOT_READ: u64 = 0;

/// The watch over a set of exports, on a thread of its own, until it is dropped.
pub struct Watch {
    stop: Arc<AtomicBool>,
}

impl Watch {
    pub fn start(exports: &[Export], skip: &[String], out: mpsc::Sender<Message>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let roots: Vec<(String, Root)> = exports
            .iter()
            .map(|export| (export.name.clone(), Root::new(&export.path)))
            .collect();
        let skip: HashSet<String> = skip.iter().cloned().collect();
        let stopping = Arc::clone(&stop);
        thread::spawn(move || run(roots, &skip, &out, &stopping));
        Self { stop }
    }
}

impl Drop for Watch {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

#[derive(Default)]
struct Owed {
    paths: HashMap<String, BTreeSet<String>>,
    resync: HashSet<String>,
}

impl Owed {
    fn note(&mut self, export: &str, path: String) {
        let paths = self.paths.entry(export.to_owned()).or_default();
        paths.insert(path);
        if paths.len() > MAX_PATHS {
            self.paths.remove(export);
            self.resync.insert(export.to_owned());
        }
    }

    fn flush(&mut self, out: &mpsc::Sender<Message>) -> bool {
        let mut open = true;
        for export in self.resync.drain() {
            self.paths.remove(&export);
            open = open && out.blocking_send(Message::text(resync(&export))).is_ok();
        }
        for (export, paths) in self.paths.drain() {
            let paths: Vec<String> = paths.into_iter().collect();
            open = open
                && out
                    .blocking_send(Message::text(changed(&export, &paths)))
                    .is_ok();
        }
        open
    }
}

fn skipped(path: &str, skip: &HashSet<String>) -> bool {
    path.split('/').any(|part| skip.contains(part))
}

// One watch per directory, ours to decide: a recursive watch gives up on the first
// directory it cannot read, and it follows links out of the tree.
fn add(watcher: &mut RecommendedWatcher, root: &Root, top: &str, skip: &HashSet<String>) {
    let mut pending = vec![top.to_owned()];
    while let Some(current) = pending.pop() {
        if let Err(err) = watcher.watch(&root.path().join(&current), RecursiveMode::NonRecursive) {
            tracing::warn!(directory = %current, error = %err, "not watching this directory");
            continue;
        }
        let listing = walk::list(root, &current, NOT_READ, false);
        pending.extend(listing.dirs.into_iter().filter(|dir| !skipped(dir, skip)));
    }
}

fn locate<'a>(roots: &'a [(String, Root)], path: &'a Path) -> Option<(&'a str, &'a Root, &'a str)> {
    roots
        .iter()
        .filter_map(|(name, root)| {
            let rel = path.strip_prefix(root.path()).ok()?.to_str()?;
            Some((name.as_str(), root, rel))
        })
        .max_by_key(|(_, root, _)| root.path().as_os_str().len())
}

// A directory that arrives whole has files nothing will report; they count as changed.
fn adopt(
    watcher: &mut RecommendedWatcher,
    (export, root): (&str, &Root),
    rel: &str,
    skip: &HashSet<String>,
    owed: &mut Owed,
) {
    add(watcher, root, rel, skip);
    walk::walk(root, rel, NOT_READ, false, &mut |listing| {
        for entry in listing.entries {
            if !skipped(&entry.path, skip) {
                owed.note(export, entry.path);
            }
        }
        true
    });
}

fn absorb(
    event: notify::Event,
    watcher: &mut RecommendedWatcher,
    roots: &[(String, Root)],
    skip: &HashSet<String>,
    owed: &mut Owed,
) {
    if event.need_rescan() {
        owed.resync
            .extend(roots.iter().map(|(name, _)| name.clone()));
        return;
    }
    if matches!(event.kind, EventKind::Access(_)) {
        return;
    }
    for path in &event.paths {
        let Some((export, root, rel)) = locate(roots, path) else {
            continue;
        };
        if rel.is_empty() || skipped(rel, skip) {
            continue;
        }
        if root.lstat(rel).is_ok_and(|meta| meta.is_dir()) {
            if matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_)) {
                adopt(watcher, (export, root), rel, skip, owed);
            }
            continue;
        }
        if !rel.rsplit('/').next().is_some_and(is_denied) {
            owed.note(export, rel.to_owned());
        }
    }
}

fn run(
    roots: Vec<(String, Root)>,
    skip: &HashSet<String>,
    out: &mpsc::Sender<Message>,
    stop: &AtomicBool,
) {
    let (events, incoming) = channel::channel();
    let mut watcher = match recommended_watcher(move |event: notify::Result<notify::Event>| {
        let _ = events.send(event);
    }) {
        Ok(watcher) => watcher,
        Err(err) => {
            tracing::warn!(error = %err, "changes on this host go unnoticed");
            return;
        }
    };
    let mut owed = Owed::default();
    for (name, root) in &roots {
        add(&mut watcher, root, "", skip);
        // Whatever changed while there was no socket, or before this watch stood.
        owed.resync.insert(name.clone());
    }
    let mut flushed = Instant::now();
    if !owed.flush(out) {
        return;
    }
    while !stop.load(Ordering::Relaxed) && !out.is_closed() {
        match incoming.recv_timeout(TICK) {
            Ok(Ok(event)) => absorb(event, &mut watcher, &roots, skip, &mut owed),
            Ok(Err(err)) => tracing::warn!(error = %err, "the watch reported an error"),
            Err(channel::RecvTimeoutError::Timeout) => {}
            Err(channel::RecvTimeoutError::Disconnected) => return,
        }
        if flushed.elapsed() >= DEBOUNCE {
            flushed = Instant::now();
            if !owed.flush(out) {
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests;
