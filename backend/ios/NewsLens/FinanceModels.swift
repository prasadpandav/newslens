import SwiftUI

/// Wire types for the finance pipeline (`/finance/*`). Kept apart from
/// Models.swift for the same reason the backend keeps `fin_*` tables apart from
/// `stories`: this is a second, domain-isolated pipeline, and a change to one
/// must not be able to break the decoding of the other.
///
/// Everything past `headline` is optional. A finance story written before a
/// field existed — or by a model that declined to fill it — has to render, and
/// the screen shows the sections it actually has rather than empty scaffolding.

struct FinanceStoriesResponse: Codable { var stories: [FinanceStoryCard] }

/// List shape. Deliberately does NOT carry the metric or actor tables: the
/// detail endpoint sends those, and inlining them in a list is how /signals
/// grew to 107KB.
struct FinanceStoryCard: Codable, Identifiable, Hashable {
    var id: String
    var headline: String
    var eventType: String?
    var narrative: String?
    var credibility: Double?
    var sectors: [String]?
    var tickers: [String]?
    var metricCount: Int?
    var sentimentNet: Double?
    var sentimentDispersion: Double?
    var imageUrl: String?
    var updatedAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, credibility, sectors, tickers
        case eventType = "event_type"
        case metricCount = "metric_count"
        case sentimentNet = "sentiment_net"
        case sentimentDispersion = "sentiment_dispersion"
        case imageUrl = "image_url"
        case updatedAt = "updated_at"
    }
}

struct FinanceStory: Codable, Identifiable, Hashable {
    var id: String
    var headline: String
    var narrative: String?
    var whyMatters: String?
    var eventType: String?
    var topic: String?
    var credibility: Double?
    var credibilityNote: String?
    var sectors: [String]?
    var geographies: [String]?
    var tickers: [String]?
    var sources: [String]?
    var economicDrivers: [String]?
    var metrics: [FinanceMetric]?
    var sentiment: FinanceSentiment?
    var entities: FinanceEntityTable?
    var relationships: [FinanceRelationship]?
    var beats: [FinanceBeat]?
    var imageUrl: String?
    var createdAt: Double?
    var updatedAt: Double?

    /// Prose to render: the beats when the model produced a real structure,
    /// otherwise the flat narrative split on blank lines. Mirrors
    /// StoryDetail.readerBeats — a story told before beats existed still reads.
    var readerBeats: [FinanceBeat] {
        if let b = beats, b.count >= 2 { return b }
        let paras = (narrative ?? "")
            .components(separatedBy: "\n\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return paras.map { FinanceBeat(label: "", text: $0) }
    }

    /// Figures worth putting in the strip. A metric with no value is a date or
    /// a label the model could not quantify — real, but not a number to show.
    var keyMetrics: [FinanceMetric] { (metrics ?? []).filter { $0.value != nil } }

    var actors: [FinanceActor] { sentiment?.actors ?? [] }

    /// How far apart the actors are. Above this the split IS the story, so the
    /// section leads with the disagreement rather than with a net score.
    var actorsDisagree: Bool { (sentiment?.dispersion ?? 0) > 0.35 }

    enum CodingKeys: String, CodingKey {
        case id, headline, narrative, topic, credibility, sectors, geographies
        case tickers, sources, metrics, sentiment, entities, relationships, beats
        case whyMatters = "why_matters"
        case eventType = "event_type"
        case credibilityNote = "credibility_note"
        case economicDrivers = "economic_drivers"
        case imageUrl = "image_url"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

struct FinanceBeat: Codable, Hashable {
    var label: String
    var text: String
}

/// One extracted figure. `verbatim` is the span it was read from — the whole
/// anti-hallucination contract of the pipeline — so it is shown, not hidden:
/// the reader can check the number against the words it came from.
struct FinanceMetric: Codable, Hashable, Identifiable {
    var name: String
    var value: Double?
    var unit: String?
    var currency: String?
    var period: String?
    var basis: String?
    var entity: String?
    var yoyChangePct: Double?
    var direction: String?
    var verbatim: String?

    var id: String { "\(name)-\(value ?? 0)-\(entity ?? "")" }

    /// "Fine amount", "Net profit" — the pipeline's snake_case field names are
    /// an internal vocabulary, not something to put in front of a reader.
    var label: String {
        name.replacingOccurrences(of: "_", with: " ").capitalizedFirst
    }

    /// "Rs 75 crore", "6.7%". Indian units are written the Indian way because
    /// that is how every source in the finance feed writes them; converting
    /// crore to millions would be a silent change to a reported figure.
    var display: String {
        guard let v = value else { return "—" }
        let n = v == v.rounded() && abs(v) < 1e9
            ? String(Int(v))
            : String(format: "%.1f", v)
        let u = (unit ?? "").lowercased()
        if u == "percent" { return "\(n)%" }
        let sym = (currency ?? "").uppercased() == "INR" ? "Rs "
                : (currency ?? "").uppercased() == "USD" ? "$" : ""
        let tail = (u.isEmpty || u == "none" || u == "count") ? "" : " \(u)"
        return "\(sym)\(n)\(tail)"
    }

    /// Guidance and consensus are not reported results. A figure whose basis is
    /// anything other than "reported" is marked, because a reader who takes a
    /// forecast for a result has been misled by the layout.
    var basisNote: String? {
        let b = (basis ?? "").lowercased()
        guard !b.isEmpty, b != "reported", b != "unknown" else { return nil }
        return b.replacingOccurrences(of: "_", with: " ")
    }

    enum CodingKeys: String, CodingKey {
        case name, value, unit, currency, period, basis, entity, direction, verbatim
        case yoyChangePct = "yoy_change_pct"
    }
}

struct FinanceSentiment: Codable, Hashable {
    var actors: [FinanceActor]?
    var rationale: String?

    var dispersion: Double {
        let vals = (actors ?? []).map(\.sentiment)
        guard vals.count > 1 else { return 0 }
        let mean = vals.reduce(0, +) / Double(vals.count)
        let variance = vals.reduce(0) { $0 + pow($1 - mean, 2) } / Double(vals.count)
        return variance.squareRoot()
    }
}

/// One simulated perspective. `incentive` is what separates this from ordinary
/// sentiment: the model had to state why this actor would read the event that
/// way before scoring it, so the reader sees the reasoning, not just the number.
struct FinanceActor: Codable, Hashable, Identifiable {
    var actor: String
    var role: String?
    var statedPosition: String?
    var inferredIncentive: String?
    var sentiment: Double
    var confidence: Double?

    var id: String { actor }

    var roleLabel: String {
        (role ?? "").replacingOccurrences(of: "_", with: " ").capitalizedFirst
    }

    /// The perspective-taking step is stored as "knows | wants". Only the wants
    /// half belongs on the card — the knows half restates the article.
    var wants: String? {
        guard let i = inferredIncentive, !i.isEmpty else { return nil }
        let parts = i.components(separatedBy: " | ")
        return (parts.count > 1 ? parts[1] : parts[0])
            .trimmingCharacters(in: .whitespaces)
    }

    enum CodingKeys: String, CodingKey {
        case actor, role, sentiment, confidence
        case statedPosition = "stated_position"
        case inferredIncentive = "inferred_incentive"
    }
}

struct FinanceEntityTable: Codable, Hashable {
    var rows: [FinanceEntityRow]?
    var unresolved: [String]?
}

struct FinanceEntityRow: Codable, Hashable, Identifiable {
    var name: String
    var type: String?
    var roleInStory: String?
    var ticker: FinanceTicker?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, type, ticker
        case roleInStory = "role_in_story"
    }
}

struct FinanceTicker: Codable, Hashable {
    var symbol: String?
    var exchange: String?
    var resolved: Bool?
}

struct FinanceRelationship: Codable, Hashable, Identifiable {
    /// Canonical graph ids — "HDFCBANK", "RBI". They are what joins edges
    /// together and what a walk matches on; they are not what a reader reads.
    var subject: String
    var predicate: String
    var object: String
    /// The names as the reporting wrote them. Absent on older responses, in
    /// which case the id stands in rather than the row disappearing.
    var subjectName: String?
    var objectName: String?
    var confidence: Double?

    var id: String { "\(subject)-\(predicate)-\(object)" }

    var verb: String { predicate.replacingOccurrences(of: "_", with: " ") }
    var from: String { subjectName ?? subject }
    var to: String { objectName ?? object }

    enum CodingKeys: String, CodingKey {
        case subject, predicate, object, confidence
        case subjectName = "subject_name"
        case objectName = "object_name"
    }
}

extension String {
    /// Uppercases the first character only — `.capitalized` would turn
    /// "sell_side analyst" into "Sell_Side Analyst" and "RBI" into "Rbi".
    var capitalizedFirst: String {
        guard let f = first else { return self }
        return String(f).uppercased() + dropFirst()
    }
}

/// The event schemas the finance pipeline classifies into, with the words a
/// reader uses for them. Unknown values fall through to a cleaned-up form of
/// whatever the server sent, so a new event type added on the backend shows up
/// as readable text instead of disappearing.
enum FinanceEventType {
    static func label(_ raw: String?) -> String {
        let known = [
            "merger_acquisition": "Deal",
            "earnings": "Earnings",
            "guidance": "Guidance",
            "regulatory_action": "Regulator",
            "leadership_change": "Leadership",
            "supply_chain": "Supply chain",
            "funding_round": "Funding",
            "debt_ratings": "Ratings",
            "legal_action": "Legal",
            "restructuring": "Restructuring",
            "product_launch": "Launch",
            "macro_policy": "Policy",
        ]
        let key = (raw ?? "").lowercased()
        if let hit = known[key] { return hit }
        if key.isEmpty || key == "other" { return "Finance" }
        return key.replacingOccurrences(of: "_", with: " ").capitalizedFirst
    }
}
