# RSS qualification addendum 3 — Dolby Vision / HDR depth

Deep-dive prompted by: which DV/HDR detail can RSS supply, and where does it
collide with the existing DV FEL feature and the Compare/conflict analyzer?

Method: 150 titles from the three DV-densest feeds (`movies_2160p`,
`movies_remux`, `tv_2160p`), plus their description bodies. Same read-only
constraints, probe's own request_feed reused.

---

## 1. THE CRUX: no DV layer information exists anywhere

Across 150 UHD titles and their description bodies:

    FEL            0
    MEL            0
    Profile 5      0
    Profile 7      0
    Profile 8      0
    single-layer   0
    dual-layer     0
    --> TOTAL layer-type mentions: 0 / 150 titles, 0 / 150 bodies

RSS cannot answer the question the DV FEL feature exists to answer. The host
detector running against the actual file stays the **only** source of
FEL / MEL / P8 / P5, and RSS cannot substitute for it in any way. RSS is at most
a hint that DV is *present*; it never states which layer.

## 2. DV is not a boolean — it is three states

`movies_2160p`, of 50 UHD releases:

    asserts DV (DV or DoVi) : 22
    asserts SDR (explicit)  :  2   <- reliable NEGATIVE
    says nothing at all     : 26   <- majority

Silence is the majority and cannot be read as "no DV". Proof — same film, same
night, one labelled and one not:

    The.Devil.Wears.Prada.2.2026.Hybrid.2160p.UHD.Blu-ray.Remux.HEVC.DV.HDR10P…   <- DV
    The.Devil.Wears.Prada.2.2026.2160p.UHD.BluRay.H265-GAZPROM – 47.1 GB          <- silent

But a useful counter-case — the same group publishing both variants as a pair,
where silence genuinely means no DV:

    Heartstopper.S03.DV.2160p.WEB.h265-BETTY – 37.7 GB
    Heartstopper.S03.2160p.WEB.h265-BETTY    – 29.8 GB

Required semantics, three states:

    dv_claim: 'asserted' | 'negated' | 'unknown'   (unknown is the DEFAULT)

`negated` only when SDR is explicit, or when the same group published a
distinguished DV/non-DV pair. **`unknown` must never collapse to `negated`.**

## 3. SAFETY: RSS DV must not feed scoring as a boolean

Two consumers are dangerous if silence is coerced to false:

**Plex labeler.** If RSS `dv=false` reached the closed-set labeler, 26 of 50 UHD
releases would be mislabeled from a metadata absence.

**Compare modal / conflict analyzer.** This is the one to guard hardest. There
was already a near-miss where scoring would have recommended overwriting 4K DV
with 1080p. If RSS DV becomes a scoring input and silence reads as "no DV", a
genuinely-DV 4K release loses to a labelled 1080p one and the recommendation is
to overwrite the better file — the same failure class, a new input.

Rule: the conflict analyzer and Plex labeler consume `dv_claim` only as
{asserted, negated} and must treat `unknown` as "insufficient evidence, do not
downgrade", never as a negative signal. The authoritative DV state for any file
already in the library remains the host detector, not RSS.

## 4. Two parser traps confirmed from live data

Notation counts, `movies_2160p`:

    DoVi=4  DV=18  HDR10+=5  HDR10P=4  HDR10=1  HDR(bare)=10  SDR=2

**Trap A — HDR10+ has two spellings.** `HDR10+` (5) and `HDR10P` (4) both occur
and mean the same thing. A parser matching only `HDR10\+` under-counts HDR10+ by
roughly half. Match both `HDR10\+` and `\bHDR10P\b`.

**Trap B — `DV` needs a word boundary.** The feed uses `DV`, `DoVi`, and
`Dolby.Vision` interchangeably — all three must match, and `DV` specifically as
`\bDV\b` or it false-positives inside release-group names and other tokens.

These join the previously reported precedence trap
(`S\d+E\d+` before bare `S\d+`) and the size en-dash trap
(`–` U+2013, not `-`) as the four naming traps confirmed from live data. All
four belong in the parser's regression suite as explicit cases.

## 5. HDR beyond DV

Bare `HDR` (10), `HDR10` (1), `HLG` (present in TV feeds). HDR type is a small
closed set — {SDR, HDR10, HDR10+, HLG, DV, DV+HDR10+} — and titles that carry it
carry it in a parseable form. But like DV, absence is not SDR; only an explicit
`SDR` token is a reliable negative.

## 6. Net for the schema

    dv_claim   'asserted' | 'negated' | 'unknown'   default 'unknown'
    hdr_type   nullable text (closed set)           default NULL, not 'SDR'
    hevc       tri-state                             absent != false

None of these may be NOT NULL, and none may default to a negative. The host
detector, not RSS, owns the authoritative DV layer for library files.
