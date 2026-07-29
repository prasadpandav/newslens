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

    struct Location: Codable {
        var city: String = ""
        var region: String = ""
        var country: String = ""
    }

    enum CodingKeys: String, CodingKey {
        case interests, profession, location, micro
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

struct FeedItem: Codable, Identifiable, Hashable {
    var id: String
    var headline: String
    var narrative: String
    var credibility: Double
    var credibilityNote: String?
    var topic: String
    var impactText: String?
    var impactScore: Int?
    // Optional: only /feed and /stories return it; /signals & /trend stories omit it.
    var createdAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, credibility, topic
        case credibilityNote = "credibility_note"
        case impactText = "impact_text"
        case impactScore = "impact_score"
        case createdAt = "created_at"
    }
}

// MARK: - Trends

struct TrendsResponse: Codable { var items: [Trend] }

struct Trend: Codable, Identifiable, Hashable {
    var id: String
    var kind: String            // "macro" | "micro"
    var name: String
    var narrative: String
    var sectors: [String]?
    var velocity: Double?
    var articleCount: Int?
    var createdAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, narrative, sectors, velocity
        case articleCount = "article_count"
        case createdAt = "created_at"
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

struct StoryDetail: Codable {
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
    var createdAt: Double?
    /// "Why it matters", stored separately from the storyline. Absent/empty on
    /// stories written before the split — callers fall back to the old
    /// first-paragraph/rest division of `narrative`.
    var whyMatters: String?

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
        case credibilityNote = "credibility_note"
        case impactText = "impact_text"
        case impactScore = "impact_score"
        case createdAt = "created_at"
        case whyMatters = "why_matters"
    }
}
