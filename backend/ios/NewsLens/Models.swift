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

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, credibility, topic
        case credibilityNote = "credibility_note"
        case impactText = "impact_text"
        case impactScore = "impact_score"
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

    enum CodingKeys: String, CodingKey {
        case id, kind, name, narrative, sectors, velocity
        case articleCount = "article_count"
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
    }
}
