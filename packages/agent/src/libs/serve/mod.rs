use std::collections::HashMap;
use std::io::{self, Write};
use std::os::unix::fs::MetadataExt;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::{mpsc, OwnedSemaphorePermit, Semaphore};
use tokio_tungstenite::tungstenite::Message;

use crate::libs::confine::{is_denied, Root, Unread};
use crate::libs::exports::Export;
use crate::libs::grep::{self, Hit, Query};
use crate::libs::protocol::{data, failure, success, Limits, Request};
use crate::libs::walk::{self, Entry, Listing, Skipped};

const MAX_RUNNING: usize = 64;
const MAX_PER_MINUTE: u32 = 120_000;
const MINUTE: Duration = Duration::from_secs(60);
const MAX_BATCH_ITEMS: usize = 500;
// The stack refuses a text frame past 1 MiB; an item is weighed as it is written.
const MAX_BATCH_BYTES: usize = 256 * 1024;
const CHUNK_BYTES: usize = 256 * 1024;
const MAX_STAT_PATHS: usize = 128;
const MAX_FETCH_PATHS: usize = 4096;
const MAX_PATTERN_BYTES: usize = 8 * 1024;
const MAX_IGNORE_LINES: usize = 10_000;
const GREP_WITHIN: Duration = Duration::from_secs(60);

const BUSY: &str = "busy";
const BAD_ARGS: &str = "bad_args";
const UNKNOWN_OP: &str = "unknown_op";
const UNKNOWN_EXPORT: &str = "unknown_export";

const fn yes() -> bool {
    true
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WalkArgs {
    #[allow(
        dead_code,
        reason = "named so that deny_unknown_fields lets it through"
    )]
    export: String,
    #[serde(default)]
    top: String,
    #[serde(default = "yes")]
    deep: bool,
    #[serde(default = "yes")]
    md5: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PathsArgs {
    #[allow(
        dead_code,
        reason = "named so that deny_unknown_fields lets it through"
    )]
    export: String,
    paths: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadArgs {
    #[allow(
        dead_code,
        reason = "named so that deny_unknown_fields lets it through"
    )]
    export: String,
    path: String,
    #[serde(default)]
    limit: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HeadArgs {
    #[allow(
        dead_code,
        reason = "named so that deny_unknown_fields lets it through"
    )]
    export: String,
    path: String,
    n: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GrepArgs {
    #[allow(
        dead_code,
        reason = "named so that deny_unknown_fields lets it through"
    )]
    export: String,
    pattern: String,
    #[serde(default)]
    ignore_lines: Vec<String>,
    #[serde(default)]
    path_glob: Option<String>,
    limit: usize,
    #[serde(default)]
    fixed: bool,
}

#[derive(Serialize)]
struct Stat {
    path: String,
    is_file: bool,
    is_dir: bool,
    size: Option<u64>,
    contained: bool,
}

/// Where a running operation answers: dropped by the socket, it stops being sent to.
struct Reply {
    id: u32,
    out: mpsc::Sender<Message>,
}

impl Reply {
    fn text(&self, text: String) -> bool {
        self.out.blocking_send(Message::text(text)).is_ok()
    }

    fn done(&self, result: &Value) -> bool {
        self.text(success(self.id, result, false))
    }

    fn part(&self, result: &Value) -> bool {
        self.text(success(self.id, result, true))
    }

    fn refuse(&self, code: &str, message: &str) -> bool {
        self.text(failure(self.id, code, message))
    }

    fn unread(&self, why: Unread) -> bool {
        self.refuse(why.code(), why.code())
    }

    fn bytes(&self, last: bool, payload: &[u8]) -> bool {
        self.out
            .blocking_send(Message::binary(data(self.id, last, payload)))
            .is_ok()
    }

    fn all_bytes(&self, bytes: &[u8]) -> bool {
        let mut chunks = bytes.chunks(CHUNK_BYTES).peekable();
        if chunks.peek().is_none() {
            return self.bytes(true, &[]);
        }
        while let Some(chunk) = chunks.next() {
            if !self.bytes(chunks.peek().is_none(), chunk) {
                return false;
            }
        }
        true
    }
}

#[derive(Default)]
struct WalkBatch {
    entries: Vec<Entry>,
    dirs: Vec<String>,
    skipped: Vec<Skipped>,
    bytes: usize,
}

impl WalkBatch {
    fn value(&mut self) -> Value {
        let batch = std::mem::take(self);
        json!({"entries": batch.entries, "dirs": batch.dirs, "skipped": batch.skipped})
    }

    fn items(&self) -> usize {
        self.entries.len() + self.dirs.len() + self.skipped.len()
    }

    fn full(&self) -> bool {
        self.items() >= MAX_BATCH_ITEMS || self.bytes >= MAX_BATCH_BYTES
    }
}

// As many bytes as an item takes in the frame: a quote or a control byte in a name
// is written as several, which a count of the name's own bytes misses.
fn weight<T: Serialize>(item: &T) -> usize {
    serde_json::to_vec(item).map_or(MAX_BATCH_BYTES, |written| written.len() + 1)
}

fn send_listing(batch: &mut WalkBatch, listing: Listing, reply: &Reply) -> bool {
    for entry in listing.entries {
        batch.bytes += weight(&entry);
        batch.entries.push(entry);
        if batch.full() && !reply.part(&batch.value()) {
            return false;
        }
    }
    for dir in listing.dirs {
        batch.bytes += weight(&dir);
        batch.dirs.push(dir);
        if batch.full() && !reply.part(&batch.value()) {
            return false;
        }
    }
    for skipped in listing.skipped {
        batch.bytes += weight(&skipped);
        batch.skipped.push(skipped);
        if batch.full() && !reply.part(&batch.value()) {
            return false;
        }
    }
    true
}

fn walk_op(root: &Root, args: &WalkArgs, limits: &Limits, reply: &Reply) {
    let mut batch = WalkBatch::default();
    let mut open = true;
    if args.deep {
        walk::walk(
            root,
            &args.top,
            limits.max_file_bytes,
            args.md5,
            &mut |listing| {
                open = send_listing(&mut batch, listing, reply);
                open
            },
        );
    } else {
        let listing = walk::list(root, &args.top, limits.max_file_bytes, args.md5);
        open = send_listing(&mut batch, listing, reply);
    }
    if open {
        reply.done(&batch.value());
    }
}

fn stat_one(root: &Root, path: &str) -> Stat {
    let contained = root.contains(path);
    // A denied name answers as a path with nothing at it.
    let denied = path.rsplit('/').next().is_some_and(is_denied);
    let found = if contained && !denied {
        root.stat(path).ok()
    } else {
        None
    };
    Stat {
        path: path.to_owned(),
        is_file: found.as_ref().is_some_and(std::fs::Metadata::is_file),
        is_dir: found.as_ref().is_some_and(std::fs::Metadata::is_dir),
        size: found.as_ref().map(MetadataExt::size),
        contained,
    }
}

fn stat_op(root: &Root, args: &PathsArgs, reply: &Reply) {
    if args.paths.len() > MAX_STAT_PATHS {
        reply.refuse(BAD_ARGS, "too many paths");
        return;
    }
    let stats: Vec<Stat> = args.paths.iter().map(|path| stat_one(root, path)).collect();
    reply.done(&json!({"stats": stats}));
}

fn read_op(root: &Root, args: &ReadArgs, limits: &Limits, reply: &Reply) {
    match root.read(&args.path, limits.max_file_bytes) {
        Ok(bytes) => {
            let keep = if args.limit == 0 {
                bytes.len()
            } else {
                usize::try_from(args.limit)
                    .unwrap_or(usize::MAX)
                    .min(bytes.len())
            };
            reply.all_bytes(&bytes[..keep]);
        }
        Err(why) => {
            reply.unread(why);
        }
    }
}

fn head_op(root: &Root, args: &HeadArgs, limits: &Limits, reply: &Reply) {
    match root.head(&args.path, args.n.min(limits.max_file_bytes)) {
        Ok(bytes) => {
            reply.all_bytes(&bytes);
        }
        Err(why) => {
            reply.unread(why);
        }
    }
}

fn grep_op(root: &Root, args: GrepArgs, limits: &Limits, reply: &Reply) {
    if args.pattern.len() > MAX_PATTERN_BYTES || args.ignore_lines.len() > MAX_IGNORE_LINES {
        reply.refuse(BAD_ARGS, "the pattern or the ignore lines are too long");
        return;
    }
    let query = Query {
        pattern: args.pattern,
        ignore_lines: args.ignore_lines,
        path_glob: args.path_glob,
        limit: args.limit,
        fixed: args.fixed,
        within: GREP_WITHIN,
        max_file_bytes: limits.max_file_bytes,
    };
    let mut batch: Vec<Hit> = Vec::new();
    let mut bytes = 0;
    let mut open = true;
    let outcome = grep::search(root, &query, &mut |hit| {
        bytes += weight(&hit);
        batch.push(hit);
        if batch.len() >= MAX_BATCH_ITEMS || bytes >= MAX_BATCH_BYTES {
            open = reply.part(&json!({"matches": batch}));
            batch.clear();
            bytes = 0;
        }
        open
    });
    match outcome {
        Err(message) => {
            reply.refuse(BAD_ARGS, &message);
        }
        Ok(()) if open => {
            reply.done(&json!({"matches": batch}));
        }
        Ok(()) => {}
    }
}

struct FrameWriter<'a> {
    reply: &'a Reply,
    pending: Vec<u8>,
}

impl Write for FrameWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.pending.extend_from_slice(bytes);
        if self.pending.len() >= CHUNK_BYTES {
            self.flush()?;
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let sent = self.reply.bytes(false, &self.pending);
        self.pending.clear();
        if sent {
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "the socket is gone",
            ))
        }
    }
}

// A file that cannot be handed over is left out of the archive, and what asked sees it missing.
fn fetch_op(root: &Root, args: &PathsArgs, limits: &Limits, reply: &Reply) {
    if args.paths.len() > MAX_FETCH_PATHS {
        reply.refuse(BAD_ARGS, "too many paths");
        return;
    }
    let writer = FrameWriter {
        reply,
        pending: Vec::new(),
    };
    let mut archive = tar::Builder::new(writer);
    for path in &args.paths {
        let Ok(bytes) = root.read(path, limits.max_file_bytes) else {
            continue;
        };
        let mut header = tar::Header::new_gnu();
        header.set_size(bytes.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        if archive
            .append_data(&mut header, path, bytes.as_slice())
            .is_err()
        {
            return;
        }
    }
    if let Ok(mut writer) = archive.into_inner() {
        if writer.flush().is_ok() {
            reply.bytes(true, &[]);
        }
    }
}

fn parsed<T: DeserializeOwned>(args: Value, reply: &Reply) -> Option<T> {
    match serde_json::from_value(args) {
        Ok(parsed) => Some(parsed),
        Err(err) => {
            reply.refuse(BAD_ARGS, &err.to_string());
            None
        }
    }
}

fn run(op: &str, args: Value, root: &Root, limits: &Limits, reply: &Reply) {
    match op {
        "walk" => {
            if let Some(args) = parsed(args, reply) {
                walk_op(root, &args, limits, reply);
            }
        }
        "stat" => {
            if let Some(args) = parsed(args, reply) {
                stat_op(root, &args, reply);
            }
        }
        "read" => {
            if let Some(args) = parsed(args, reply) {
                read_op(root, &args, limits, reply);
            }
        }
        "head" => {
            if let Some(args) = parsed(args, reply) {
                head_op(root, &args, limits, reply);
            }
        }
        "grep" => {
            if let Some(args) = parsed(args, reply) {
                grep_op(root, args, limits, reply);
            }
        }
        "fetch" => {
            if let Some(args) = parsed(args, reply) {
                fetch_op(root, &args, limits, reply);
            }
        }
        _ => {
            reply.refuse(UNKNOWN_OP, "no such operation");
        }
    }
}

fn is_known(op: &str) -> bool {
    matches!(op, "walk" | "stat" | "read" | "head" | "grep" | "fetch")
}

fn roots_of(exports: &[Export]) -> HashMap<String, Root> {
    exports
        .iter()
        .map(|export| (export.name.clone(), Root::new(&export.path)))
        .collect()
}

/// Answers the stack's requests. There is no operation that writes, and none is added here.
pub struct Server {
    roots: RwLock<Arc<HashMap<String, Root>>>,
    limits: Limits,
    out: mpsc::Sender<Message>,
    slots: Arc<Semaphore>,
    window: Mutex<(Instant, u32)>,
}

impl Server {
    pub fn new(exports: &[Export], limits: Limits, out: mpsc::Sender<Message>) -> Self {
        Self {
            roots: RwLock::new(Arc::new(roots_of(exports))),
            limits,
            out,
            slots: Arc::new(Semaphore::new(MAX_RUNNING)),
            window: Mutex::new((Instant::now(), 0)),
        }
    }

    /// What is being served changes for the next request; one already running ends as it began.
    pub fn set_exports(&self, exports: &[Export]) {
        *self.roots.write().expect("roots") = Arc::new(roots_of(exports));
    }

    fn spend(&self) -> bool {
        let mut window = self.window.lock().expect("window");
        if window.0.elapsed() >= MINUTE {
            *window = (Instant::now(), 0);
        }
        if window.1 >= MAX_PER_MINUTE {
            return false;
        }
        window.1 += 1;
        true
    }

    fn answer_now(&self, id: u32, code: &'static str, message: &'static str) {
        let out = self.out.clone();
        tokio::spawn(async move {
            let _ = out.send(Message::text(failure(id, code, message))).await;
        });
    }

    fn slot(&self) -> Option<OwnedSemaphorePermit> {
        Arc::clone(&self.slots).try_acquire_owned().ok()
    }

    /// Never waits: what is past the cap answers `busy`, and the stack comes back later.
    pub fn dispatch(&self, request: Request) {
        let Request { id, op, args } = request;
        if !is_known(&op) {
            return self.answer_now(id, UNKNOWN_OP, "no such operation");
        }
        if !self.spend() {
            return self.answer_now(id, BUSY, "over the request budget");
        }
        let Some(permit) = self.slot() else {
            return self.answer_now(id, BUSY, "too many requests running");
        };
        let export = args.get("export").and_then(Value::as_str);
        let roots = self.roots.read().expect("roots").clone();
        let Some(root) = export.and_then(|name| roots.get(name)).cloned() else {
            return self.answer_now(id, UNKNOWN_EXPORT, "no such export");
        };
        let limits = self.limits.clone();
        let reply = Reply {
            id,
            out: self.out.clone(),
        };
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            run(&op, args, &root, &limits, &reply);
        });
    }
}

#[cfg(test)]
mod tests;
