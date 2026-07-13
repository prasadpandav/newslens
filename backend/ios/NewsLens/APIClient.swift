import Foundation
import Combine

/// Talks to the NewsLens backend. Change `baseURL` to your server.
/// For beta the token lives in UserDefaults; move to Keychain before wider release.
@MainActor
final class APIClient: ObservableObject {
    static let shared = APIClient()

    var baseURL = URL(string: "http://localhost:8000")!

    @Published var userID: String? = UserDefaults.standard.string(forKey: "user_id")
    private var token: String? = UserDefaults.standard.string(forKey: "token")

    struct APIError: Error { let message: String }

    private struct AnyEncodable: Encodable {
        let value: any Encodable
        func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
    }

    private func request(_ path: String, method: String = "GET",
                         body: (any Encodable)? = nil,
                         query: [String: String] = [:]) async throws -> Data {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(value: body))
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIError(message: "Server error for \(path)")
        }
        return data
    }

    // MARK: - Users & context

    func ensureUser() async throws {
        guard userID == nil else { return }
        struct NewUser: Codable { let user_id: String; let token: String }
        let data = try await request("users", method: "POST")
        let u = try JSONDecoder().decode(NewUser.self, from: data)
        userID = u.user_id
        token = u.token
        UserDefaults.standard.set(u.user_id, forKey: "user_id")
        UserDefaults.standard.set(u.token, forKey: "token")
    }

    func saveContext(_ ctx: UserContext) async throws {
        try await ensureUser()
        guard let userID else { return }
        _ = try await request("users/\(userID)/context", method: "PUT", body: ctx)
        UserDefaults.standard.set(true, forKey: "onboarded")
    }

    // MARK: - Content

    func fetchFeed() async throws -> [FeedItem] {
        if let userID {
            if let data = try? await request("feed", query: ["user_id": userID]) {
                return try JSONDecoder().decode(FeedResponse.self, from: data).items
            }
            // The stored user no longer exists (e.g. backend DB was reset):
            // clear stale credentials and re-run onboarding on next launch.
            clearStaleUser()
        }
        // Fallback: public stories (no personalization yet)
        let data = try await request("stories")
        return try JSONDecoder().decode(FeedResponse.self, from: data).items
    }

    private func clearStaleUser() {
        userID = nil
        token = nil
        let d = UserDefaults.standard
        d.removeObject(forKey: "user_id")
        d.removeObject(forKey: "token")
        d.set(false, forKey: "onboarded")
    }

    func fetchTrendDetail(id: String) async throws -> TrendDetail {
        let data = try await request("trend/\(id)")
        return try JSONDecoder().decode(TrendDetail.self, from: data)
    }

    func fetchStory(id: String) async throws -> StoryDetail {
        let data = try await request("story/\(id)", query: ["user_id": userID ?? ""])
        return try JSONDecoder().decode(StoryDetail.self, from: data)
    }

    func fetchTrends() async throws -> [Trend] {
        let data = try await request("trends")
        return try JSONDecoder().decode(TrendsResponse.self, from: data).items
    }

    func ask(_ question: String, storyID: String?) async throws -> AskResponse {
        struct Body: Codable { let question: String; let story_id: String; let user_id: String }
        let data = try await request("ask", method: "POST",
                                     body: Body(question: question,
                                                story_id: storyID ?? "",
                                                user_id: userID ?? ""))
        return try JSONDecoder().decode(AskResponse.self, from: data)
    }

    func sendFeedback(storyID: String, action: String) async {
        guard let userID else { return }
        _ = try? await request("feedback", method: "POST",
                               query: ["user_id": userID, "story_id": storyID,
                                       "action": action])
    }
}

// MARK: - Engagement (learning-framed, stored locally)

@MainActor
final class Engagement: ObservableObject {
    static let shared = Engagement()
    @Published var streak: Int
    @Published var understood: Int
    @Published var topics: Set<String>

    private init() {
        let d = UserDefaults.standard
        streak = max(1, d.integer(forKey: "eng_streak"))
        understood = d.integer(forKey: "eng_understood")
        topics = Set(d.stringArray(forKey: "eng_topics") ?? [])
        bumpStreak()
    }

    private func bumpStreak() {
        let d = UserDefaults.standard
        let today = Calendar.current.startOfDay(for: .now)
        let last = d.object(forKey: "eng_lastDay") as? Date ?? .distantPast
        if !Calendar.current.isDate(last, inSameDayAs: today) {
            let yesterday = Calendar.current.date(byAdding: .day, value: -1, to: today)!
            streak = Calendar.current.isDate(last, inSameDayAs: yesterday) ? streak + 1 : 1
            d.set(today, forKey: "eng_lastDay")
            d.set(streak, forKey: "eng_streak")
        }
    }

    func storyUnderstood() {
        understood += 1
        UserDefaults.standard.set(understood, forKey: "eng_understood")
    }

    func explored(topic: String) {
        topics.insert(topic)
        UserDefaults.standard.set(Array(topics), forKey: "eng_topics")
    }
}
