//! Hashed TF-IDF vectors: the always-present embedder behind ABP's memory recall, the
//! knowledge base and the file-server index (`bot/fileserver/index.py`).
//!
//! Ported from the pure-Python `_features`/`hash_vectors` pair, and bit-for-bit identical
//! to it: same features in the same order, the same f32 rounding after every addition, and
//! the same pairwise summation numpy's `add.reduce` uses for the norm (its accumulation
//! order is part of the result, so it is reproduced rather than approximated).

use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;
use std::borrow::Cow;
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

pub const DIM: usize = 1024;
const MAX_WORDS: usize = 20_000;

/// FxHash-style hasher for the feature table. The keys are the caller's own text (and a
/// bounded length), so this is a speed choice, not a collision-risk one.
#[derive(Default)]
struct FastHasher(u64);

impl Hasher for FastHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.0 = (self.0.rotate_left(5) ^ u64::from(byte)).wrapping_mul(0x517c_c1b7_2722_0a95);
        }
    }
}

/// `[\w']+` runs, lowercased, capped at the first `MAX_WORDS` of them.
fn words(text: &str) -> Vec<Cow<'_, str>> {
    crate::word_runs()
        .find_iter(text)
        .take(MAX_WORDS)
        .map(|hit| {
            let word = hit.as_str();
            if word.chars().any(char::is_uppercase) {
                Cow::Owned(word.to_lowercase())
            } else {
                Cow::Borrowed(word)
            }
        })
        .collect()
}

/// One text's feature counts, in the order the Python dict inserted them (which is the
/// order they are accumulated into the vector in, so it is part of the result).
#[derive(Default)]
struct Features {
    keys: Vec<Box<[u8]>>,
    counts: Vec<f64>,
    seen: HashMap<Box<[u8]>, usize, BuildHasherDefault<FastHasher>>,
}

impl Features {
    /// `key` is a scratch buffer: it is only copied when the feature is new, so the common
    /// case (a feature seen again) allocates nothing.
    fn add(&mut self, key: &[u8], by: f64) {
        match self.seen.get(key) {
            Some(&at) => self.counts[at] += by,
            None => {
                let owned: Box<[u8]> = key.to_vec().into_boxed_slice();
                self.seen.insert(owned.clone(), self.keys.len());
                self.keys.push(owned);
                self.counts.push(by);
            }
        }
    }
}

/// `hashlib.blake2b(feature, digest_size=8)`, read as a little-endian u64.
fn feature_hash(key: &[u8]) -> u64 {
    let mut hasher = Blake2bVar::new(8).expect("8 is a valid BLAKE2b digest size");
    hasher.update(key);
    let mut digest = [0u8; 8];
    hasher
        .finalize_variable(&mut digest)
        .expect("8 bytes always fit");
    u64::from_le_bytes(digest)
}

/// numpy's `add.reduce` over a float32 array: pairwise, blocks of 128, eight accumulators.
fn pairwise_sum(a: &[f32]) -> f32 {
    const BLOCK: usize = 128;
    let n = a.len();
    if n < 8 {
        let mut res = 0.0f32;
        for &x in &a[..n] {
            res += x;
        }
        return res;
    }
    if n <= BLOCK {
        let mut r = [0.0f32; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        let aligned = n - (n % 8);
        while i < aligned {
            for (slot, value) in r.iter_mut().zip(&a[i..i + 8]) {
                *slot += *value;
            }
            i += 8;
        }
        // numpy's grouping exactly. Rust would read this chain left to right, which is a
        // different sum and, at float32, a different answer.
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        return res;
    }
    let mut half = n / 2;
    half -= half % 8;
    pairwise_sum(&a[..half]) + pairwise_sum(&a[half..])
}

/// One text as `DIM` little-endian float32 values: the signed feature hashing, then L2
/// normalised by `np.maximum(norm, 1e-9)` exactly as the Python does.
fn one(text: &str) -> Vec<u8> {
    let ws = words(text);
    let mut feats = Features::default();
    let mut key: Vec<u8> = Vec::with_capacity(64);
    let mut padded = String::with_capacity(64);
    let mut bounds: Vec<usize> = Vec::with_capacity(64);

    for w in &ws {
        key.clear();
        key.extend_from_slice(b"w:");
        key.extend_from_slice(w.as_bytes());
        feats.add(&key, 1.0);

        // "#word#", then every three-character window of it. Sliced on character
        // boundaries, so a multi-byte letter is a window like any other.
        padded.clear();
        padded.push('#');
        padded.push_str(w);
        padded.push('#');
        bounds.clear();
        bounds.extend(padded.char_indices().map(|(at, _)| at));
        bounds.push(padded.len());
        for window in 0..bounds.len().saturating_sub(3) {
            key.clear();
            key.extend_from_slice(b"c:");
            key.extend_from_slice(&padded.as_bytes()[bounds[window]..bounds[window + 3]]);
            feats.add(&key, 0.3);
        }
    }
    for pair in ws.windows(2) {
        key.clear();
        key.extend_from_slice(b"b:");
        key.extend_from_slice(pair[0].as_bytes());
        key.push(b' ');
        key.extend_from_slice(pair[1].as_bytes());
        feats.add(&key, 0.7);
    }

    let mut acc = [0.0f32; DIM];
    for (key, &count) in feats.keys.iter().zip(&feats.counts) {
        let h = feature_hash(key);
        let signed = 1.0 + count.ln();
        // numpy casts the float64 term to float32 before the float32 addition, so we do too.
        let term = if (h >> 63) & 1 == 1 {
            signed as f32
        } else {
            -(signed as f32)
        };
        let bucket = (h % DIM as u64) as usize;
        acc[bucket] += term;
    }

    let squares: Vec<f32> = acc.iter().map(|v| v * v).collect();
    let norm = pairwise_sum(&squares).sqrt();
    let floor = 1e-9f32;
    let divisor = if norm > floor { norm } else { floor };

    let mut out = Vec::with_capacity(DIM * 4);
    for value in acc {
        out.extend_from_slice(&(value / divisor).to_le_bytes());
    }
    out
}

/// Every text's vector, in the order given.
pub fn hash_vectors(texts: &[String]) -> Vec<Vec<u8>> {
    texts.iter().map(|t| one(t)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blake2b_matches_the_digest_sizes_python_asks_for() {
        // BLAKE2b-512("abc"), the published known answer.
        let mut hasher = Blake2bVar::new(64).unwrap();
        hasher.update(b"abc");
        let mut digest = [0u8; 64];
        hasher.finalize_variable(&mut digest).unwrap();
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex,
            "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1\
             7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"
        );
        // The digest length is part of BLAKE2b's parameter block, so 8 bytes is its own
        // function, not a truncation of the 64-byte one.
        assert_ne!(
            feature_hash(b"abc"),
            u64::from_le_bytes(digest[..8].try_into().unwrap())
        );
    }

    #[test]
    fn the_feature_hash_is_blake2b_8_little_endian() {
        // What Python's hashlib.blake2b(b"w:the", digest_size=8) reads as a little-endian u64.
        assert_eq!(feature_hash(b"w:the"), 8_088_518_962_037_187_430);
        assert_eq!(feature_hash("c:#th".as_bytes()), 11_838_791_933_447_345_648);
    }

    #[test]
    fn pairwise_sum_keeps_numpy_add_reduce_summation_order() {
        // Exact where it can be: 1024 ones, and a recursion boundary (1024 -> 512 -> 256 -> 128).
        assert_eq!(pairwise_sum(&[1.0f32; DIM]), 1024.0);
        assert_eq!(pairwise_sum(&[1.0f32; 7]), 7.0);
        assert_eq!(pairwise_sum(&[1.0f32; 129]), 129.0);
        // The order matters: [1e8, 1, 1, ...] saturates in float32 but not in float64, and the
        // eight-accumulator split is what decides where it stops mattering.
        let mut a = vec![0.0f32; DIM];
        a[0] = 1e8;
        a[1] = 1.0;
        assert_eq!(pairwise_sum(&a), 1e8);
        // ...which the Python parity tests pin against numpy for real vectors.
    }

    #[test]
    fn a_text_with_features_matches_the_python_vectors_first_floats() {
        // The first four float32 values of index.hash_vectors() for one sentence.
        let out = one("The home server is called Server and runs on the LAN with a static lease.");
        assert_eq!(&out[..8], &[0x08, 0x46, 0x20, 0x3d, 0, 0, 0, 0]);
    }

    #[test]
    fn words_stop_at_the_python_cap() {
        let many = "w ".repeat(MAX_WORDS + 50);
        assert_eq!(words(&many).len(), MAX_WORDS);
        assert_eq!(
            words("don't stop_now, 42 times"),
            vec!["don't", "stop_now", "42", "times"]
        );
    }

    #[test]
    fn a_text_with_no_words_is_all_zeroes() {
        let out = one("   ...   ");
        assert_eq!(out.len(), DIM * 4);
        assert!(out.iter().all(|b| *b == 0));
    }
}
