use std::fmt;
use std::fs::{self, File, Metadata};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::path::{Component, Path, PathBuf};

use rustix::fs::{open, openat2, Mode, OFlags, ResolveFlags};
use rustix::io::Errno;

// Kept equal to CONTENT_DENIED_NAMES in packages/core/src/enggraph/core/config.py by a test.
pub const DENIED_NAMES: &[&str] = &[
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jwt",
    "*.tfvars",
    ".htpasswd",
    "authorized_keys",
    "credentials",
    "id_rsa*",
    "id_ed25519*",
];

const MAX_PATH: usize = 4096;
const OPEN_RETRIES: u32 = 8;

/// Why a path gave no file. `Outside` is confinement, `Io` is an ordinary failure.
#[derive(Debug)]
pub enum Refusal {
    Outside,
    Io(io::Error),
}

impl fmt::Display for Refusal {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Outside => out.write_str("outside the root"),
            Self::Io(err) => write!(out, "{err}"),
        }
    }
}

impl From<io::Error> for Refusal {
    fn from(err: io::Error) -> Self {
        Self::Io(err)
    }
}

impl From<Errno> for Refusal {
    fn from(err: Errno) -> Self {
        Self::Io(err.into())
    }
}

/// Why a file's bytes were not handed over; the code goes on the wire.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Unread {
    Denied,
    Oversized,
    Outside,
    Unreadable,
}

impl Unread {
    pub fn code(self) -> &'static str {
        match self {
            Self::Denied => "denied",
            Self::Oversized => "over the size limit",
            Self::Outside => "outside",
            Self::Unreadable => "unreadable",
        }
    }
}

fn unread(refusal: Refusal) -> Unread {
    match refusal {
        Refusal::Outside => Unread::Outside,
        Refusal::Io(_) => Unread::Unreadable,
    }
}

// fnmatch for the shapes the list holds: a literal, or one star anywhere.
fn matches(pattern: &str, name: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == name,
        Some((head, tail)) => {
            name.len() >= head.len() + tail.len() && name.starts_with(head) && name.ends_with(tail)
        }
    }
}

pub fn is_denied(name: &str) -> bool {
    DENIED_NAMES.iter().any(|pattern| matches(pattern, name))
}

fn basename(raw: &str) -> &str {
    raw.rsplit('/').next().unwrap_or(raw)
}

/// Refuses an absolute path and any `..`; what is left can only name something below.
pub fn relative(raw: &str) -> Result<PathBuf, Refusal> {
    if raw.len() > MAX_PATH || raw.contains('\0') {
        return Err(Refusal::Outside);
    }
    let mut clean = PathBuf::new();
    for component in Path::new(raw).components() {
        match component {
            Component::Normal(part) => clean.push(part),
            Component::CurDir => {}
            _ => return Err(Refusal::Outside),
        }
    }
    Ok(clean)
}

/// One export's directory. Every path into it comes through here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Root {
    path: PathBuf,
}

impl Root {
    /// Takes a path already made canonical when the export was loaded.
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn resolve(&self, rel: &Path, flags: OFlags) -> Result<File, Refusal> {
        let base = File::open(&self.path)?;
        let target = if rel.as_os_str().is_empty() {
            Path::new(".")
        } else {
            rel
        };
        let beneath = ResolveFlags::BENEATH | ResolveFlags::NO_MAGICLINKS;
        for _ in 0..OPEN_RETRIES {
            match openat2(&base, target, flags, Mode::empty(), beneath) {
                Ok(fd) => return Ok(File::from(fd)),
                // A rename raced the walk; the kernel asks to try again.
                Err(Errno::AGAIN) => {}
                // No openat2 here: an old kernel, or a sandbox that filters it.
                Err(Errno::NOSYS | Errno::PERM) => return self.fallback(rel, flags),
                Err(Errno::XDEV | Errno::LOOP) => return Err(Refusal::Outside),
                Err(err) => return Err(err.into()),
            }
        }
        Err(Errno::AGAIN.into())
    }

    fn fallback(&self, rel: &Path, flags: OFlags) -> Result<File, Refusal> {
        let root = fs::canonicalize(&self.path)?;
        let full = self.path.join(rel);
        let target = match (full.parent(), full.file_name()) {
            (Some(parent), Some(name))
                if flags.contains(OFlags::NOFOLLOW) && !rel.as_os_str().is_empty() =>
            {
                fs::canonicalize(parent)?.join(name)
            }
            _ => fs::canonicalize(&full)?,
        };
        if !target.starts_with(&root) {
            return Err(Refusal::Outside);
        }
        let fd = open(&target, flags | OFlags::NOFOLLOW, Mode::empty())?;
        Ok(File::from(fd))
    }

    // O_NONBLOCK: a FIFO in a tree would otherwise hold this thread until a writer came.
    fn read_flags() -> OFlags {
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOCTTY | OFlags::NONBLOCK
    }

    /// Opens a file to read, following links only while they stay inside the root.
    pub fn open(&self, raw: &str) -> Result<File, Refusal> {
        self.resolve(&relative(raw)?, Self::read_flags())
    }

    /// Opens a directory to list, never through a link.
    pub fn open_dir(&self, raw: &str) -> Result<File, Refusal> {
        let flags = Self::read_flags() | OFlags::DIRECTORY | OFlags::NOFOLLOW;
        self.resolve(&relative(raw)?, flags)
    }

    /// What a path is, links followed as far as the root allows.
    pub fn stat(&self, raw: &str) -> Result<Metadata, Refusal> {
        let file = self.resolve(&relative(raw)?, OFlags::PATH | OFlags::CLOEXEC)?;
        Ok(file.metadata()?)
    }

    /// What a path itself is: a link answers as a link.
    pub fn lstat(&self, raw: &str) -> Result<Metadata, Refusal> {
        let flags = OFlags::PATH | OFlags::CLOEXEC | OFlags::NOFOLLOW;
        let file = self.resolve(&relative(raw)?, flags)?;
        Ok(file.metadata()?)
    }

    /// A path that does not exist is still inside; one that climbs or links out is not.
    pub fn contains(&self, raw: &str) -> bool {
        !matches!(self.stat(raw), Err(Refusal::Outside))
    }

    // A link's own name says nothing: `notes.txt -> .env` must not hand over the key.
    fn names_denied(&self, raw: &str, file: &File) -> bool {
        let opened = fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd()))
            .or_else(|_| fs::canonicalize(self.path.join(raw)));
        match opened {
            Ok(path) => path
                .file_name()
                .is_some_and(|name| is_denied(&name.to_string_lossy())),
            Err(_) => true,
        }
    }

    fn open_to_read(&self, raw: &str) -> Result<(File, Metadata), Unread> {
        if is_denied(basename(raw)) {
            return Err(Unread::Denied);
        }
        let file = self.open(raw).map_err(unread)?;
        if self.names_denied(raw, &file) {
            return Err(Unread::Denied);
        }
        let meta = file.metadata().map_err(|_| Unread::Unreadable)?;
        if !meta.is_file() {
            return Err(Unread::Unreadable);
        }
        Ok((file, meta))
    }

    /// A file's bytes, the size limit checked against what is read as well as what stat said.
    pub fn read(&self, raw: &str, max_bytes: u64) -> Result<Vec<u8>, Unread> {
        let (file, meta) = self.open_to_read(raw)?;
        if meta.len() > max_bytes {
            return Err(Unread::Oversized);
        }
        let mut bytes = Vec::new();
        file.take(max_bytes + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| Unread::Unreadable)?;
        if bytes.len() as u64 > max_bytes {
            return Err(Unread::Oversized);
        }
        Ok(bytes)
    }

    /// The first bytes of a file, whatever its size.
    pub fn head(&self, raw: &str, count: u64) -> Result<Vec<u8>, Unread> {
        let (file, _) = self.open_to_read(raw)?;
        let mut bytes = Vec::new();
        file.take(count)
            .read_to_end(&mut bytes)
            .map_err(|_| Unread::Unreadable)?;
        Ok(bytes)
    }
}

#[cfg(test)]
mod tests;
