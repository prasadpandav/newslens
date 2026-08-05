import SwiftUI

// MARK: - The agreement scale
//
// PLAIN WORDS, NOT JARGON is a stated rule of the design: "Nothing says
// 'corroborated', 'verified' or 'disputed' — the scale is written as a plain
// sentence." These are the mockups' five sentences, and the thresholds
// reproduce every worked example in them (21 → Almost no proof, 58 → Some
// sources agree, 79 and 82 → Most sources agree, 94 → Nearly all sources agree).
//
// This is a deliberate duplicate of the web portal's `FX.BANDS`. Two clients
// reading one number and printing different sentences for it would be worse
// than the duplication, so the values are kept identical and this comment says
// where the other copy lives: `web/index.html`, `const FX`.

struct AgreementBand {
    enum Tone { case good, mid, bad }

    let name: String
    /// How many of the five pips are lit.
    let lit: Int
    let tone: Tone

    private static let ladder: [(min: Double, band: AgreementBand)] = [
        (85, .init(name: "Nearly all sources agree", lit: 5, tone: .good)),
        (70, .init(name: "Most sources agree",       lit: 4, tone: .good)),
        (55, .init(name: "Some sources agree",       lit: 3, tone: .mid)),
        (40, .init(name: "Sources disagree",         lit: 2, tone: .mid)),
        (-1, .init(name: "Almost no proof",          lit: 1, tone: .bad)),
    ]

    static func of(_ credibility: Double) -> AgreementBand {
        let c = (credibility.isFinite ? credibility : 0).rounded()
        return ladder.first { c >= $0.min }!.band
    }

    /// "Most sources agree · 82" — the sentence leads and the number follows it
    /// inside the same coloured unit. The score never appears on its own.
    static func sentence(_ credibility: Double) -> String {
        let c = Int((credibility.isFinite ? credibility : 0).rounded())
        return "\(of(credibility).name) · \(c)"
    }
}

// MARK: - Pips

/// The five-segment agreement mark. Lit segments take the verdict's FILL and
/// the label beside them takes the verdict's TEXT colour — see `Palette`'s note
/// on why those are two different values.
struct PipScale: View {
    var credibility: Double
    /// The mockups draw these at 12×5 on a hero and 9–10×4 on a row.
    var width: CGFloat = 12
    var height: CGFloat = 5
    @Environment(\.palette) private var pal

    var body: some View {
        let band = AgreementBand.of(credibility)
        HStack(spacing: 2) {
            ForEach(1...5, id: \.self) { n in
                RoundedRectangle(cornerRadius: pal.radius == 0 ? 0 : 2)
                    .fill(n <= band.lit ? pal.credFill(credibility) : pal.hairline2)
                    .frame(width: width, height: height)
            }
        }
        .accessibilityHidden(true)   // the sentence beside it carries the meaning
    }
}

/// Pips followed by "Most sources agree · 82", as one unit.
struct AgreementLine: View {
    var credibility: Double
    /// 13 on a hero, 12.5 on a list row — the mockups' own two values. This is
    /// the sentence the whole card is built to make readable, so it is set at
    /// interface-label size rather than caption size.
    var size: CGFloat = 13
    var pipWidth: CGFloat = 12
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 7) {
            PipScale(credibility: credibility, width: pipWidth,
                     height: pipWidth < 11 ? 4 : 5)
            Text(AgreementBand.sentence(credibility))
                .font(pal.sans(size, .medium))
                .foregroundStyle(pal.credColor(credibility))
                .lineLimit(1)
                .minimumScaleFactor(0.85)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(AgreementBand.sentence(credibility))
    }
}

// MARK: - Evidence on a story

/// The counts the redesign puts on the surface of a card. Every one of these is
/// optional on the wire and stays optional here: the backend omits a key rather
/// than sending a zero, precisely so a client can tell "no facts are argued
/// over" from "we never checked". Collapsing that to `?? 0` would erase the
/// distinction the whole panel exists to make.
protocol EvidenceCarrying {
    var credibility: Double { get }
    var createdAt: Double? { get }
    var updatedAt: Double? { get }
    var narrative: String { get }
    var claimsVerified: Int? { get }
    var claimsDisputed: Int? { get }
    var claimsUnverified: Int? { get }
    var claimsTotal: Int? { get }
    var sourceCount: Int? { get }
    var sourceKinds: [String: Int]? { get }
    var sourcePrimary: Int? { get }
    var conflicts: Int? { get }
}

extension EvidenceCarrying {
    var band: AgreementBand { .of(credibility) }

    /// "Developing" is a real, cheap signal rather than a label anyone assigns:
    /// the Storyteller UPDATES a story in place when new articles land on the
    /// same event, so `updated_at` pulling away from `created_at` means the
    /// storyline moved after it was first told.
    var isDeveloping: Bool { ((updatedAt ?? 0) - (createdAt ?? 0)) > 3600 }

    var wordCount: Int {
        narrative.split(whereSeparator: \.isWhitespace).count
    }
    var readingMinutes: Int { max(1, Int((Double(wordCount) / 200).rounded())) }

    /// "6 of 8 checked" — nil when this story has no verdicts at all, so the
    /// line is absent rather than reading "0 of 0".
    var factsCheckedLine: String? {
        guard let total = claimsTotal, total > 0 else { return nil }
        let checked = (claimsVerified ?? 0) + (claimsDisputed ?? 0)
        return "\(checked) of \(total) checked"
    }

    /// The row flag: a verdict fact in rust, or a calm note when it is only
    /// timing. Returns nil when there is nothing true to say.
    var rowFlag: (text: String, calm: Bool)? {
        if let d = claimsDisputed, d > 0 {
            return ("\(d) fact\(d == 1 ? "" : "s") argued over", false)
        }
        if sourceCount == 1 { return ("only one source says this", false) }
        if isDeveloping, let u = updatedAt {
            return ("updated \(Ago.short(u))", true)
        }
        return nil
    }
}

// MARK: - How likely a forecast is

/// The likelihood scale, as a word first. "72% confidence" is a number about
/// our own model dressed up as a fact about the world; the design replaces it
/// with what the number actually means and keeps the figure as a footnote.
/// Same thresholds as the web portal's `ODDS`.
enum Odds {
    static func word(_ confidence: Double) -> String {
        let p = percent(confidence)
        return p >= 65 ? "Likely" : p >= 40 ? "Could go either way" : "Unlikely"
    }
    static func percent(_ confidence: Double) -> Int {
        Int(((confidence.isFinite ? confidence : 0) * 100).rounded())
    }
    static func color(_ confidence: Double, _ pal: Palette) -> Color {
        let p = percent(confidence)
        return p >= 65 ? pal.trust : p >= 40 ? pal.warning : pal.breaking
    }
}

/// A worded horizon turned into a real date. "6-9 months" from a forecast made
/// in August becomes "By May 2027".
///
/// Returns nil when the horizon carries no number the reader could check
/// ("soon", "medium term"), in which case the raw phrase is printed instead of
/// a date we invented. Mirrors the web portal's `BY_WHEN`.
enum Horizon {
    static func byWhen(_ horizon: String?, from: Double?) -> String? {
        guard let horizon, !horizon.isEmpty else { return nil }
        let text = horizon.lowercased()
        // Which unit the phrase is measured in. Checked longest-first so
        // "18 months" can't be read as "1 month".
        let unit: String
        if text.contains("year") { unit = "year" }
        else if text.contains("month") { unit = "month" }
        else if text.contains("week") { unit = "week" }
        else { return nil }   // "soon", "medium term" — no date to compute
        // The LAST number in a range is the one that has to pass: "6-9 months"
        // is not settled until month nine.
        let numbers = text.split(whereSeparator: { !$0.isNumber })
            .compactMap { Int($0) }
        guard let n = numbers.last, n > 0 else { return nil }
        let days = unit == "year" ? n * 365 : unit == "week" ? n * 7 : n * 30
        let base = (from ?? Date().timeIntervalSince1970) + Double(days) * 86_400
        return "By " + Date(timeIntervalSince1970: base)
            .formatted(.dateTime.month(.abbreviated).year())
    }

    /// Forecasts measured in years get the lighter "Further out" treatment.
    static func isDistant(_ horizon: String?) -> Bool {
        (horizon ?? "").range(of: "year", options: .caseInsensitive) != nil
    }
}

// MARK: - Does this touch the reader?

/// The free, no-LLM relevance test: does this item's own words mention anything
/// the reader told us they care about?
///
/// Deliberately not the personalizer — that costs an LLM call per story and is
/// only worth spending when someone opens the panel. This is a substring match
/// over interests and location, which is enough to badge a card "near you" and
/// to count "3 matter to you". Same test the web portal runs (`TOUCHES`).
enum Lens {
    static var context: UserContext? {
        guard let d = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: d)
    }

    static func touches(_ text: String) -> Bool {
        guard let c = context else { return false }
        let needles = (c.interests + [c.location.city, c.location.region, c.location.country])
            .map { $0.lowercased() }
            .filter { $0.count > 2 }
        guard !needles.isEmpty else { return false }
        let hay = text.lowercased()
        return needles.contains { hay.contains($0) }
    }
}

// MARK: - What kind of sources

/// The outlet mix behind a set of stories, from the source-kind axis in
/// `sources.yaml`. Plain words, as the design requires: a trade outlet is one
/// "with something to gain", not a "trade publication".
///
/// `primary` ("original documents") is deliberately not surfaced. The
/// `kinds.primary` list in sources.yaml is empty — we ingest no primary-document
/// feeds — so that figure is 0 for every story in the catalogue. The mockups
/// print "11 original documents"; printing our real 0 on every card would read
/// as a failure rather than as the absence of a feed, so the row is left out
/// until there is something to put in it.
struct SourceMix {
    var counts: [String: Int] = [:]

    init(_ stories: [FeedItem]) {
        for s in stories {
            for (kind, n) in s.sourceKinds ?? [:] {
                counts[kind, default: 0] += n
            }
        }
    }

    var total: Int { counts.values.reduce(0, +) }
    var newsrooms: Int { counts["newsroom"] ?? 0 }
    /// Outlets that stand to benefit if you believe the story.
    var interested: Int { counts["trade"] ?? 0 }

    /// "5 sources have something to gain" — only when there are any, and only
    /// when it is a meaningful share. A single trade outlet among twenty
    /// newsrooms is not a finding.
    var interestedWarning: String? {
        guard interested > 0, total > 0, Double(interested) / Double(total) >= 0.25
        else { return nil }
        return "\(interested) source\(interested == 1 ? "" : "s") ha\(interested == 1 ? "s" : "ve") something to gain"
    }
}

// MARK: - Where a trend is in its life

/// "Strengthening · 6 weeks", "Sources disagree · 4 weeks", "Newly forming ·
/// 9 days" — all from real timestamps and real counts, never a label anyone
/// assigned. Ported from the web portal's `Trends.status`.
struct TrendStatus {
    enum Kind { case new, split, strong, fading }
    let kind: Kind
    let word: String
    let note: String

    var tone: AgreementBand.Tone {
        switch kind {
        case .strong: return .good
        case .split:  return .bad
        default:      return .mid
        }
    }

    static func of(_ t: Trend) -> TrendStatus {
        // A trend with no usable created_at gets NO age claim rather than one
        // measured from the epoch — that printed "2953 weeks running".
        let born = (t.createdAt ?? 0) > 0 ? t.createdAt : nil
        let days = born.map { max(0, (Date().timeIntervalSince1970 - $0) / 86_400) }
        let weeks = days.map { max(1, Int(($0 / 7).rounded())) }
        let wk = weeks.map { "\($0) week\($0 == 1 ? "" : "s")" } ?? "running now"

        if (t.storyCount ?? 0) == 0 {
            return .init(kind: .fading, word: "Fading", note: "no scored story this week")
        }
        if t.kind == "micro" || (days.map { $0 < 10 } ?? false) {
            // "1 day old", not "1 days old" — the web copy of this rule has the
            // same bug and should be fixed alongside it.
            let note = days.map { d -> String in
                let n = max(1, Int(d.rounded()))
                return "\(n) day\(n == 1 ? "" : "s") old"
            }
            return .init(kind: .new, word: "Newly forming", note: note ?? "just appeared")
        }
        // Disagreement leads when the sources really are split — either the
        // claims came back contested, or the mean agreement is low.
        let disagree = t.disagree ?? 0, agree = t.agree ?? 0
        if disagree > 0, Double(disagree) >= Double(agree) * 0.5 {
            return .init(kind: .split, word: "Sources disagree", note: wk)
        }
        if let c = t.credibility, c < 55 {
            return .init(kind: .split, word: "Sources disagree", note: wk)
        }
        return .init(kind: .strong, word: "Strengthening",
                     note: weeks == nil ? "running now" : "\(wk) running")
    }
}

/// "How strong is this trend" — how much there is to lean on.
///
/// Deliberately conservative: the median agreement score in this catalogue is
/// around 25, so a trend has to be genuinely well covered before this says
/// "Strong". Ported from the web portal's `Trends.strength`.
struct TrendStrength {
    let lit: Int
    let word: String
    let tone: AgreementBand.Tone
    let why: String

    static func of(_ t: Trend) -> TrendStrength {
        let n = t.storyCount ?? 0, a = t.articleCount ?? 0
        guard n > 0, let c = t.credibility else {
            return .init(lit: 1, word: "Early", tone: .bad,
                         why: "Not enough scored stories yet to say how well this holds up.")
        }
        var p = 1
        if n >= 2, a >= 3 { p = 2 }
        if n >= 3, a >= 5 { p = 3 }
        if n >= 5, a >= 8,  c >= 55 { p = 4 }
        if n >= 8, a >= 12, c >= 70 { p = 5 }
        if p >= 4 {
            return .init(lit: p, word: "Strong", tone: .good,
                         why: "Many stories, many independent sources, and they mostly agree.")
        }
        if p == 3 {
            return .init(lit: p, word: "Moderate", tone: .mid,
                         why: "Plenty of stories, but the sources do not fully line up.")
        }
        return .init(lit: p, word: "Early", tone: .bad,
                     why: "Few stories so far — too early to lean on.")
    }
}

/// A full-width segmented meter — the trend strength and forecast confidence
/// bars, which unlike `PipScale` stretch to fill their row.
struct SegmentBar: View {
    var lit: Int
    var total: Int = 5
    var color: Color
    var track: Color
    var height: CGFloat = 8
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<total, id: \.self) { i in
                RoundedRectangle(cornerRadius: pal.radius == 0 ? 0 : 2)
                    .fill(i < lit ? color : track)
                    .frame(height: height)
            }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - Relative time

enum Ago {
    /// "just now", "12 min ago", "3h ago", "2d ago" — the web portal's `FX.ago`.
    static func short(_ epochSeconds: Double?) -> String {
        guard let epochSeconds, epochSeconds > 0 else { return "" }
        let m = Int((Date().timeIntervalSince1970 - epochSeconds) / 60)
        if m < 1 { return "just now" }
        if m < 60 { return "\(m) min ago" }
        let h = m / 60
        return h < 24 ? "\(h)h ago" : "\(h / 24)d ago"
    }
}

// MARK: - Proof notes

/// One checked claim, as the margin prints it.
///
/// The mockups' own labels, verbatim. Nothing in the reader says verified,
/// corroborated or disputed — "We could not check this" is the honest third
/// state, and it says whose failing it is.
/// `nonisolated` because the model layer builds these: `StoryDetail.proofNotes`
/// is a plain computed property on a Codable struct, and this target compiles
/// with main-actor isolation by default, which would otherwise make a value
/// type's initialiser unreachable from it.
nonisolated struct ProofNote: Identifiable {
    enum Tone { case ok, mid, bad }

    let id = UUID()
    let label: String
    let tone: Tone
    let claim: String
    let note: String

    init(_ verdict: StoryDetail.Verdict) {
        let v = verdict.verdict.lowercased()
        if v.contains("corrob") {
            label = "Checked — true"; tone = .ok
        } else if v.contains("disput") {
            label = "Sources disagree"; tone = .mid
        } else {
            label = "We could not check this"; tone = .bad
        }
        claim = verdict.claim
        note = verdict.note
    }

    func color(_ pal: Palette) -> Color {
        switch tone {
        case .ok:  return pal.trust
        case .mid: return pal.warning
        case .bad: return pal.breaking
        }
    }
    func tick(_ pal: Palette) -> Color {
        switch tone {
        case .ok:  return pal.goodFill
        case .mid: return pal.midFill
        case .bad: return pal.badFill
        }
    }
}
