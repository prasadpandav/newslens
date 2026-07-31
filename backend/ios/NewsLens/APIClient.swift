import Foundation
import Combine

/// Talks to the Descry backend. Change `baseURL` to your server.
/// For beta the token lives in UserDefaults; move to Keychain before wider release.
@MainActor
final class APIClient: ObservableObject {
    static let shared = APIClient()

    // Production server. For local backend testing use "http://localhost:8000"
    // (Simulator only) and remember to switch back before building for device.
    var baseURL = URL(string: "https://newslens-rmv6.onrender.com")!

    // Public web portal — used for share links. Set to your static site URL.
    var webBaseURL = "https://www.descry.in"

    func shareURL(_ path: String) -> URL {
        URL(string: "\(webBaseURL)/#/\(path)")!
    }

    @Published var userID: String? = UserDefaults.standard.string(forKey: "user_id")
    @Published var displayName: String? = UserDefaults.standard.string(forKey: "display_name")
    @Published var userEmail: String? = UserDefaults.standard.string(forKey: "user_email")
    @Published var userPhotoURL: String? = UserDefaults.standard.string(forKey: "user_photo")

    /// True only for Google-authenticated users (guests get anonymous accounts
    /// that also have a userID, so userID alone can't distinguish them).
    var isGoogleUser: Bool { userEmail?.isEmpty == false }
    @Published var savedStoryIDs: Set<String> = []
    private var token: String? = UserDefaults.standard.string(forKey: "token")
    /// Read-only access for the SSE stream client (same module).
    var authToken: String? { token }

    struct APIError: Error { let status: Int; let message: String }

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
        req.timeoutInterval = 75   // free-tier servers can take ~60s to wake from idle
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(value: body))
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            throw APIError(status: status, message: "Server error \(status) for \(path)")
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
        // PUT replaces the whole context blob, so the theme has to ride along on
        // every write or the server's ContextIn default silently resets it. The
        // caller shouldn't have to remember this, so it is stamped here.
        var ctx = ctx
        ctx.theme = ThemeStore.shared.skin.rawValue
        _ = try await request("users/\(userID)/context", method: "PUT", body: ctx)
        UserDefaults.standard.set(true, forKey: "onboarded")
        UserDefaults.standard.set(ctx.interests, forKey: "user_interests")
        // Keep the full context locally so we can silently restore the account
        // if the backend database is ever reset (free hosting wipes on redeploy).
        if let data = try? JSONEncoder().encode(ctx) {
            UserDefaults.standard.set(data, forKey: "saved_context")
        }
    }

    // MARK: - Content

    func fetchFeed() async throws -> [FeedItem] {
        if let uid = userID {
            do {
                let data = try await request("feed", query: ["user_id": uid])
                return try JSONDecoder().decode(FeedResponse.self, from: data).items
            } catch let e as APIError where e.status == 404 || e.status == 401 {
                // The server genuinely lost our user (DB reset). Heal silently:
                // recreate the account from the locally saved context.
                await healStaleUser()
                if let uid2 = userID,
                   let data = try? await request("feed", query: ["user_id": uid2]) {
                    return try JSONDecoder().decode(FeedResponse.self, from: data).items
                }
            } catch {
                // Transient problem (timeout, cold start, no network):
                // do NOT touch credentials — fall through to public stories.
            }
        }
        // Fallback: public stories (no personalization yet)
        let data = try await request("stories")
        return try JSONDecoder().decode(FeedResponse.self, from: data).items
    }

    /// The backend no longer knows our user. Recreate it from the saved
    /// onboarding answers — the user never notices. Only if nothing is saved
    /// do we fall back to re-running onboarding.
    private func healStaleUser() async {
        userID = nil
        token = nil
        let d = UserDefaults.standard
        d.removeObject(forKey: "user_id")
        d.removeObject(forKey: "token")
        if let data = d.data(forKey: "saved_context"),
           let ctx = try? JSONDecoder().decode(UserContext.self, from: data) {
            try? await saveContext(ctx)   // recreates user + re-uploads context
        } else {
            d.set(false, forKey: "onboarded")
        }
    }

    // MARK: - Google sign-in

    /// Exchanges a Google ID token for Descry credentials.
    /// Returns true if the user already has a saved context on the server.
    func signInWithGoogle(idToken: String) async throws -> Bool {
        struct AuthOut: Codable {
            let user_id: String; let token: String
            let name: String?; let email: String?; let picture: String?
            let has_context: Bool
        }
        struct Body: Codable { let id_token: String }
        let data = try await request("auth/google", method: "POST",
                                     body: Body(id_token: idToken))
        let out = try JSONDecoder().decode(AuthOut.self, from: data)
        userID = out.user_id
        token = out.token
        displayName = out.name
        userEmail = out.email
        userPhotoURL = out.picture
        let d = UserDefaults.standard
        d.set(out.user_id, forKey: "user_id")
        d.set(out.token, forKey: "token")
        d.set(out.name ?? "", forKey: "display_name")
        d.set(out.email ?? "", forKey: "user_email")
        d.set(out.picture ?? "", forKey: "user_photo")
        if out.has_context {
            // Returning user on a fresh install: pull their lens down from the
            // server so Profile and personalization work immediately.
            if let ctxData = try? await request("users/\(out.user_id)/context"),
               let ctx = try? JSONDecoder().decode(UserContext.self, from: ctxData) {
                d.set(try? JSONEncoder().encode(ctx), forKey: "saved_context")
                d.set(ctx.interests, forKey: "user_interests")
                // The look follows the account, not the device — the promise the
                // web picker makes. Adopt whatever this account last chose.
                ThemeStore.shared.adopt(fromContext: ctx.theme)
            }
        }
        return out.has_context
    }

    /// Refresh name/email/photo from the server — heals profiles saved by
    /// older app versions that didn't store all fields locally.
    func refreshProfile() async {
        guard let userID else { return }
        struct ProfileOut: Codable {
            let name: String; let email: String; let picture: String
        }
        guard let data = try? await request("users/\(userID)/profile"),
              let p = try? JSONDecoder().decode(ProfileOut.self, from: data) else { return }
        if !p.name.isEmpty { displayName = p.name }
        if !p.email.isEmpty { userEmail = p.email }
        if !p.picture.isEmpty { userPhotoURL = p.picture }
        let d = UserDefaults.standard
        d.set(p.name, forKey: "display_name")
        d.set(p.email, forKey: "user_email")
        d.set(p.picture, forKey: "user_photo")
    }

    /// Sign out locally. Server-side data (context, bookmarks) stays on the
    /// account and is restored on the next Google sign-in.
    func signOut() {
        userID = nil
        token = nil
        displayName = nil
        userEmail = nil
        userPhotoURL = nil
        savedStoryIDs = []
        let d = UserDefaults.standard
        for key in ["user_id", "token", "display_name", "user_email", "user_photo",
                    "saved_context"] {
            d.removeObject(forKey: key)
        }
        // Theming is a signed-in personalization; don't keep showing a signed-out
        // visitor someone else's chosen look. Matches Theme.reset() on the web.
        ThemeStore.shared.reset()
    }

    // MARK: - Bookmarks (saved articles)

    func loadBookmarks() async {
        guard userID != nil else { return }
        if let items = try? await fetchBookmarks() {
            savedStoryIDs = Set(items.map(\.id))
        }
    }

    func fetchBookmarks() async throws -> [FeedItem] {
        try await ensureUser()
        guard let userID else { return [] }
        let data = try await request("bookmarks", query: ["user_id": userID])
        return try JSONDecoder().decode(FeedResponse.self, from: data).items
    }

    func toggleBookmark(storyID: String) async {
        try? await ensureUser()
        guard let userID else { return }
        if savedStoryIDs.contains(storyID) {
            savedStoryIDs.remove(storyID)
            _ = try? await request("bookmarks", method: "DELETE",
                                   query: ["user_id": userID, "story_id": storyID])
        } else {
            savedStoryIDs.insert(storyID)
            _ = try? await request("bookmarks", method: "POST",
                                   query: ["user_id": userID, "story_id": storyID])
        }
    }

    func fetchSignals() async throws -> [Signal] {
        // user_id as well as the bearer header: the server needs to know WHICH
        // user's token to check before it will send the signed-in-only parts of
        // a forecast (reasoning, watch-list, supporting stories).
        let data = try await request("signals", query: ["user_id": userID ?? ""])
        return try JSONDecoder().decode(SignalsResponse.self, from: data).items
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

    // MARK: - Live hero + live feed

    /// Snapshot of dynamic-hero cards (first paint + SSE fallback).
    func fetchLive(categories: [String]) async throws -> [LiveCard] {
        var q = ["categories": categories.joined(separator: ",")]
        if let uid = userID { q["user_id"] = uid }
        let data = try await request("live", query: q)
        return try JSONDecoder().decode(LiveResponse.self, from: data).items
    }

    /// Only stories newer than `since` (epoch seconds) — powers the "N new stories"
    /// banner without refetching the whole feed.
    func fetchFeed(since: Double) async throws -> [FeedItem] {
        guard let uid = userID else { return [] }
        let data = try await request("feed",
                                     query: ["user_id": uid, "since": String(since)])
        return try JSONDecoder().decode(FeedResponse.self, from: data).items
    }

    /// Persist the user's hero config (feature 5). Merges into the saved context so
    /// other fields aren't lost, then reuses saveContext (local cache + PUT).
    func saveLivePrefs(_ prefs: LivePrefs) async {
        var ctx = UserDefaults.standard.data(forKey: "saved_context")
            .flatMap { try? JSONDecoder().decode(UserContext.self, from: $0) } ?? UserContext()
        ctx.livePrefs = prefs
        try? await saveContext(ctx)
    }

    /// Raw byte stream for the SSE hero channel. Not buffered like `request`.
    func liveStreamBytes(categories: [String]) async throws
        -> (URLSession.AsyncBytes, URLResponse) {
        var comps = URLComponents(url: baseURL.appendingPathComponent("live/stream"),
                                  resolvingAgainstBaseURL: false)!
        var q = [URLQueryItem(name: "categories", value: categories.joined(separator: ","))]
        if let uid = userID { q.append(URLQueryItem(name: "user_id", value: uid)) }
        comps.queryItems = q
        var req = URLRequest(url: comps.url!)
        req.timeoutInterval = 3600
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return try await URLSession.shared.bytes(for: req)
    }
}

// MARK: - Live SSE client

/// Owns the Server-Sent-Events connection to `/live/stream`. Publishes hero cards
/// and the feed-freshness marker. Seeds from a snapshot, reconnects with backoff,
/// and falls back to snapshot polling when the stream can't hold. Drive its
/// lifecycle from the view: `start()` when visible/foregrounded, `stop()` otherwise.
@MainActor
final class LiveStream: ObservableObject {
    @Published private(set) var cards: [LiveCard] = []
    @Published private(set) var feed: FeedMarker?
    @Published private(set) var connected = false

    private unowned let api: APIClient
    private var categories: [String]
    private var task: Task<Void, Never>?

    init(api: APIClient, categories: [String]) {
        self.api = api
        self.categories = categories
    }

    func start() {
        guard task == nil else { return }
        task = Task { await run() }
    }

    func stop() {
        task?.cancel(); task = nil
        connected = false
    }

    /// Switch categories (from the config sheet) — reconnects if they changed.
    func reconfigure(categories: [String]) {
        guard categories != self.categories else { return }
        self.categories = categories
        if task != nil { stop(); start() }
    }

    private func run() async {
        var backoff = 2.0
        if let seed = try? await api.fetchLive(categories: categories) { cards = seed }
        while !Task.isCancelled {
            do {
                try await consume()
                backoff = 2.0
            } catch is CancellationError {
                return
            } catch {
                connected = false
                if let snap = try? await api.fetchLive(categories: categories) { cards = snap }
                try? await Task.sleep(for: .seconds(backoff))
                backoff = min(backoff * 2, 30)
            }
        }
    }

    private func consume() async throws {
        let (bytes, resp) = try await api.liveStreamBytes(categories: categories)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        connected = true
        var event = "message", data = ""
        for try await line in bytes.lines {
            if line.isEmpty {                       // blank line = end of one SSE frame
                dispatch(event: event, data: data)
                event = "message"; data = ""
            } else if line.hasPrefix("event:") {
                event = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                let chunk = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                data += data.isEmpty ? chunk : "\n" + chunk
            }                                        // ':' comment (heartbeat) ignored
        }
    }

    private func dispatch(event: String, data: String) {
        guard let d = data.data(using: .utf8) else { return }
        switch event {
        case "live":
            if let p = try? JSONDecoder().decode(LiveStreamPayload.self, from: d) {
                cards = p.cards
            }
        case "feed":
            if let m = try? JSONDecoder().decode(FeedMarker.self, from: d) {
                feed = m
            }
        default:
            break
        }
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
