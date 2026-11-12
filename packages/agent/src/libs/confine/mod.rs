use std::collections::VecDeque;
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs::{self, File, Metadata};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path, PathBuf};

use rustix::fs::{openat, openat2, readlinkat, Mode, OFlags, ResolveFlags};
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
// As many links as the kernel follows in one path.
const MAX_LINKS: u32 = 40;

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

    // Walks the path one component at a time, each opened below the one before and
    // never through a link, so a directory swapped for a link is not followed.
    fn fallback(&self, rel: &Path, flags: OFlags) -> Result<File, Refusal> {
        let look = OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let mut held = vec![File::open(&self.path)?];
        let mut todo: VecDeque<OsString> = rel
            .components()
            .map(|part| part.as_os_str().to_owned())
            .collect();
        let mut links = 0;
        while let Some(part) = todo.pop_front() {
            if part == OsStr::new(".") {
                continue;
            }
            if part == OsStr::new("..") {
                if held.len() == 1 {
                    return Err(Refusal::Outside);
                }
                held.pop();
                continue;
            }
            let Some(top) = held.last() else {
                return Err(Refusal::Outside);
            };
            let last = todo.is_empty();
            let seen = File::from(openat(top, part.as_os_str(), look, Mode::empty())?);
            let kind = seen.metadata()?.file_type();
            if kind.is_symlink() {
                if last && flags.contains(OFlags::NOFOLLOW) {
                    return if flags.contains(OFlags::PATH) {
                        Ok(seen)
                    } else {
                        Err(Refusal::Outside)
                    };
                }
                links += 1;
                let target = readlinkat(top, part.as_os_str(), Vec::new())?;
                let target = Path::new(OsStr::from_bytes(target.as_bytes()));
                if links > MAX_LINKS || target.is_absolute() {
                    return Err(Refusal::Outside);
                }
                for piece in target.components().rev() {
                    todo.push_front(piece.as_os_str().to_owned());
                }
                continue;
            }
            if !last {
                if !kind.is_dir() {
                    return Err(Errno::NOTDIR.into());
                }
                held.push(seen);
                continue;
            }
            if flags.contains(OFlags::PATH) {
                return Ok(seen);
            }
            // By name once more, for the access mode: a link put there since is refused.
            return match openat(
                top,
                part.as_os_str(),
                flags | OFlags::NOFOLLOW,
                Mode::empty(),
            ) {
                Ok(fd) => Ok(File::from(fd)),
                Err(Errno::LOOP) => Err(Refusal::Outside),
                Err(err) => Err(err.into()),
            };
        }
        let Some(top) = held.last() else {
            return Err(Refusal::Outside);
        };
        Ok(File::from(openat(top, ".", flags, Mode::empty())?))
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

    /// Opens a regular file whose own name, and the name it resolves to, are not denied.
    pub fn open_to_read(&self, raw: &str) -> Result<(File, Metadata), Unread> {
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
        file.take(max_bytes.saturating_add(1))
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
