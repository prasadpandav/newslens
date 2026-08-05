import Foundation

// MARK: - User context sent to backend

struct UserContext: Codable {
    var interests: [String] = []
    var profession: String = ""
    var lineOfBusiness: String = ""
    var roleSeniority: String = ""
    var location: Location = Location()
    var nativeLanguage: String = ""
    var preferredLanguage: String = "English"
    var micro: [String: String] = [:]
    // Dynamic-hero config. Optional so older stored contexts (no live_prefs) still
    // decode — the synthesized decoder treats a missing Optional key as nil.
    var livePrefs: LivePrefs?
    /// Chosen look — "default" | "journal" | "signal". This has to be modelled
    /// even though only the UI consumes it: PUT /users/{id}/context REPLACES the
    /// whole blob and the server's ContextIn.theme defaults to "default", so a
    /// field this struct doesn't carry isn't preserved across a save — it is
    /// reset. Before this existed, editing interests or the hero config on iOS
    /// silently threw away a theme picked on the web.
    ///
    /// Optional for the same reason livePrefs is: a context saved before this
    /// field existed has no `theme` key, and the synthesized decoder throws on a
    /// missing key for a non-Optional property regardless of its default value.
    var theme: String?

    struct Location: Codable {
        var city: String = ""
        var region: String = ""
        var country: String = ""
    }

    enum CodingKeys: String, CodingKey {
        case interests, profession, location, micro, theme
        case lineOfBusiness = "line_of_business"
        case roleSeniority = "role_seniority"
        case nativeLanguage = "native_language"
        case preferredLanguage = "preferred_language"
        case livePrefs = "live_prefs"
    }
}

/// User configuration for the dynamic home hero (feature 5). Encodes to the
/// backend's open `live_prefs` bag.
struct LivePrefs: Codable, Hashable {
    var enabled: Bool = true
    var categories: [String] = LiveCategory.allCases.map(\.rawValue)

    static let `default` = LivePrefs()
}

/// The four things the hero can show. Order here is the default display order.
enum LiveCategory: String, CaseIterable, Identifiable, Hashable {
    case breaking, events, sports, finance
    var id: String { rawValue }

    var label: String {
        switch self {
        case .breaking: return "Breaking"
        case .events:   return "Important events"
        case .sports:   return "Sports scores"
        case .finance:  return "Finance"
        }
    }
    var icon: String {
        switch self {
        case .breaking: return "bolt.fill"
        case .events:   return "star.fill"
        case .sports:   return "sportscourt.fill"
        case .finance:  return "chart.line.uptrend.xyaxis"
        }
    }
    /// Which live_card `type` string this category maps to.
    var cardType: String {
        switch self {
        case .breaking: return "breaking"
        case .events:   return "event"
        case .sports:   return "score"
        case .finance:  return "market"
        }
    }
    static func forCardType(_ type: String) -> LiveCategory? {
        allCases.first { $0.cardType == type }
    }
}

// MARK: - Feed

struct FeedResponse: Codable { var items: [FeedItem] }

struct FeedItem: Codable, Identifiable, Hashable, EvidenceCarrying {
    var id: String
    var headline: String
    var narrative: String
    var credibility: Double
    var credibilityNote: String?
    var topic: String
    var impactText: String?
    var impactScore: Int?
    /// Publisher artwork, URL only — the app loads it straight from their CDN.
    /// Absent on stories whose sources carried no image; the card renders
    /// text-only in that case.
    var imageUrl: String?
    // Optional: only /feed and /stories return it; /signals & /trend stories omit it.
    var createdAt: Double?
    /// Bumped when a developing storyline is retold. `updated_at != created_at`
    /// is the whole basis of the "Developing · still unfolding" kicker.
    var updatedAt: Double?

    // Evidence counts. The server has been sending these since the redesign
    // (see `_evidence` in main.py) and the app simply never decoded them —
    // which is why the cards had a percentage and no facts behind it. Every one
    // is Optional because the server OMITS a key it cannot compute rather than
    // sending a zero; see the note on `EvidenceCarrying`.
    var claimsVerified: Int?
    var claimsDisputed: Int?
    var claimsUnverified: Int?
    var claimsTotal: Int?
    var sourceCount: Int?
    var sourceKinds: [String: Int]?
    var sourcePrimary: Int?
    var conflicts: Int?
    /// Present only on stories whose own corroboration actually fell, or where
    /// a checked fact became argued over. Absent means "nothing to report",
    /// never "stable" — so nothing is drawn either way.
    var correction: Correction?

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, credibility, topic, conflicts, correction
        case credibilityNote = "credibility_note"
        case impactText = "impact_text"
        case impactScore = "impact_score"
        case imageUrl = "image_url"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case claimsVerified = "claims_verified"
        case claimsDisputed = "claims_disputed"
        case claimsUnverified = "claims_unverified"
        case claimsTotal = "claims_total"
        case sourceCount = "source_count"
        case sourceKinds = "source_kinds"
        case sourcePrimary = "source_primary"
    }
}

/// "Fewer sources agree now" — computed server-side from `story_history`.
///
/// Never called a retraction, here or in the strings the server sends: we can
/// observe OUR corroboration falling; we cannot observe a publisher withdrawing
/// anything, and the two are not the same claim.
struct Correction: Codable, Hashable {
    var kind: String
    var note: String?
    var from: Double?
    var to: Double?

    private static let headings = [
        "weakened":    "Fewer sources agree now",
        "contested":   "A fact we checked is now argued over",
        "conflicting": "Outlets now report different numbers",
    ]
    var heading: String { Self.headings[kind] ?? Self.headings["weakened"]! }
}

// MARK: - Trends

struct TrendsResponse: Codable { var items: [Trend] }

struct Trend: Codable, Identifiable, Hashable {
    var id: String
    var kind: String            // "macro" | "micro"
    var name: String
    var narrative: String
    var sectors: [String]?
    var regions: [String]?
    var velocity: Double?
    var articleCount: Int?
    var createdAt: Double?
    var updatedAt: Double?
    /// Mean agreement across the trend's stories in the feed window. `nil` —
    /// not 0 — when no scored story carries this trend, so the card can say
    /// "not enough to judge yet" instead of reporting a zero as a finding.
    var credibility: Double?
    var storyCount: Int?
    /// Sums of the per-claim verdicts on the stories underneath. Sent only when
    /// claims were actually checked: 0/0 would read as "nobody agrees" when it
    /// means "we haven't checked".
    var agree: Int?
    var disagree: Int?
    /// How many of this trend's stories have since had their own corroboration
    /// fall. Only ever a positive count — absent means "none had enough history
    /// to tell", which is far commoner than "none weakened".
    var weakenedCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, narrative, sectors, regions, velocity, credibility, agree, disagree
        case articleCount = "article_count"
        case storyCount = "story_count"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case weakenedCount = "weakened_count"
    }

    /// Everything this trend says about itself, for the free relevance test.
    var lensText: String {
        ([name, narrative] + (sectors ?? []) + (regions ?? [])).joined(separator: " ")
    }
}

// MARK: - Foresight signals

struct SignalsResponse: Codable { var items: [Signal] }

struct Signal: Codable, Identifiable, Hashable {
    var id: String
    var title: String
    var prediction: String
    var chain: String
    var watch: String
    var affected: [String]?
    var horizon: String?
    var confidence: Double
    var stories: [FeedItem]
    // Headline↔story-id pairs for turning inline story references into links.
    var storyRefs: [StoryRef]?
    var createdAt: Double?
    /// True when the server withheld the signed-in-only parts (chain, watch,
    /// stories) because the caller wasn't authenticated. `storyCount` still
    /// reports how many stories back the forecast, so the locked state can say
    /// what is waiting without having received it.
    var locked: Bool?
    var storyCount: Int?

    var isLocked: Bool { locked == true }

    /// Everything this forecast says about itself, for the free relevance test.
    var lensText: String {
        ([title, prediction] + (affected ?? [])).joined(separator: " ")
    }

    enum CodingKeys: String, CodingKey {
        case id, title, prediction, chain, watch, affected, horizon, confidence, stories, locked
        case storyRefs = "story_refs"
        case createdAt = "created_at"
        case storyCount = "story_count"
    }
}

/// Maps a story headline (as it now appears inline in linkified prose) back to
/// its id, so clients can render it as a tappable link. See backend `story_refs`.
struct StoryRef: Codable, Hashable {
    var storyID: String
    var headline: String
    enum CodingKeys: String, CodingKey {
        case storyID = "story_id"
        case headline
    }
}

// MARK: - Trend deep-dive

struct TrendDetail: Codable {
    var id: String
    var kind: String
    var name: String
    var narrative: String
    var sectors: [String]?
    var velocity: Double?
    var stories: [FeedItem]
    var storyRefs: [StoryRef]?
    var createdAt: Double?
    /// Set once the trend leaves the radar. The row is kept so existing links
    /// still open; this is what lets the screen say so instead of implying the
    /// trend is still being tracked.
    var retiredAt: Double?

    var isRetired: Bool { retiredAt != nil }

    enum CodingKeys: String, CodingKey {
        case id, kind, name, narrative, sectors, velocity, stories
        case storyRefs = "story_refs"
        case createdAt = "created_at"
        case retiredAt = "retired_at"
    }
}

// MARK: - Live dynamic hero

struct LiveResponse: Codable { var items: [LiveCard]; var enabled: Bool }

/// Payload of the SSE `live` event (shape: {"cards":[...]}).
struct LiveStreamPayload: Codable { var cards: [LiveCard] }

/// One card in the dynamic home hero. `type` is breaking | event | score | market.
/// (The server's arbitrary `payload` bag is intentionally not decoded — the hero
/// renders from title/subtitle/detail, and title already carries the score line.)
struct LiveCard: Codable, Identifiable, Hashable {
    var id: String
    var type: String
    var priority: Double
    var title: String
    var subtitle: String
    var detail: String
    var storyID: String?
    var url: String?

    enum CodingKeys: String, CodingKey {
        case id, type, priority, title, subtitle, detail, url
        case storyID = "story_id"
    }

    var category: LiveCategory? { LiveCategory.forCardType(type) }
}

/// The SSE `feed` event: cheap "is the feed newer?" signal for the new-content banner.
struct FeedMarker: Codable, Hashable {
    var count: Int
    var newestID: String
    var at: Double
    enum CodingKeys: String, CodingKey {
        case count, at
        case newestID = "newest_id"
    }
}

// MARK: - Ask AI

struct AskResponse: Codable {
    var answer: String
    var followups: [String]?
}

// MARK: - Story detail

struct StoryDetail: Codable, EvidenceCarrying {
    var id: String
    var headline: String
    var narrative: String
    var credibility: Double
    var credibilityNote: String?
    var claims: Claims?
    var topic: String
    var sources: [Source]?
    var trends: [StoryTrend]?
    var connections: [Connection]?
    var impactText: String?
    var impactScore: Int?
    var imageUrl: String?
    var createdAt: Double?
    var updatedAt: Double?
    /// "Why it matters", stored separately from the storyline. Absent/empty on
    /// stories written before the split — callers fall back to the old
    /// first-paragraph/rest division of `narrative`.
    var whyMatters: String?
    /// The storyline cut into its natural sections, each with a label written
    /// for THIS story ("Why the gap closed"). `nil` — not `[]` — on stories
    /// written before beats existed, and the reader falls back to paragraphs
    /// for those. That distinction is why this must not be defaulted.
    var beats: [Beat]?
    /// Which sentence the writer produced for each checked claim, verified
    /// server-side to occur verbatim in the prose. This is what lets the reader
    /// mark a line and attach a verdict to it honestly.
    var anchors: [Anchor]?
    var claimsVerified: Int?
    var claimsDisputed: Int?
    var claimsUnverified: Int?
    var claimsTotal: Int?
    var sourceCount: Int?
    var sourceKinds: [String: Int]?
    var sourcePrimary: Int?
    var conflicts: Int?
    var correction: Correction?

    struct Beat: Codable, Hashable {
        var label: String
        var text: String
    }
    struct Anchor: Codable, Hashable {
        /// Index into `claims.verdicts`.
        var claim: Int
        var quote: String
    }

    struct Claims: Codable {
        var claims: [String]?
        var verdicts: [Verdict]?
    }
    struct Verdict: Codable, Hashable {
        var claim: String
        var verdict: String
        var note: String
    }
    struct Source: Codable, Hashable {
        var title: String?
        var url: String?
        var source: String?
    }
    struct StoryTrend: Codable, Hashable {
        var id: String
        var kind: String
        var name: String
        var narrative: String
        var velocity: Double?
    }
    struct Connection: Codable, Hashable {
        var chain: String
        var confidence: Double
        var otherTitle: String
        var otherUrl: String
        enum CodingKeys: String, CodingKey {
            case chain, confidence
            case otherTitle = "other_title"
            case otherUrl = "other_url"
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, credibility, claims, topic, sources, trends, connections
        case beats, anchors, conflicts, correction
        case credibilityNote = "credibility_note"
        case impactText = "impact_text"
        case impactScore = "impact_score"
        case imageUrl = "image_url"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case whyMatters = "why_matters"
        case claimsVerified = "claims_verified"
        case claimsDisputed = "claims_disputed"
        case claimsUnverified = "claims_unverified"
        case claimsTotal = "claims_total"
        case sourceCount = "source_count"
        case sourceKinds = "source_kinds"
        case sourcePrimary = "source_primary"
    }

    /// The reader's sections. Beats when the story has them; otherwise the old
    /// paragraph split, so a story written before beats existed still reads as
    /// a sectioned page rather than one undifferentiated block.
    var readerBeats: [Beat] {
        if let beats, !beats.isEmpty { return beats }
        let paras = narrative.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard !paras.isEmpty else { return [] }
        // One generic label is unavoidable here — there is nothing story-specific
        // to name these with, which is exactly what beats added.
        return [Beat(label: "What happened", text: paras.joined(separator: "\n\n"))]
    }

    var verdicts: [Verdict] {
        if let v = claims?.verdicts, !v.isEmpty { return v }
        // Claims extracted but never checked: they are still real claims, and
        // saying "we could not check this" about them is truthful. Inventing a
        // verdict would not be.
        return (claims?.claims ?? []).map {
            Verdict(claim: $0, verdict: "unverified", note: "Not yet assessed")
        }
    }

    var proofNotes: [ProofNote] { verdicts.map(ProofNote.init) }
}
