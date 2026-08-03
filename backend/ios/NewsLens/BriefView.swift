import SwiftUI
import Combine

/// Daily Brief — the redesigned home. Greeting, intelligence summary, glass topic
/// filter, story cards with scroll-driven motion and zoom hero transitions.
struct BriefView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    // Live SSE channel for the hero + feed-freshness. Seeded with the shared client
    // (same instance the environment injects) so it needs no environment at init.
    @StateObject private var live = LiveStream(
        api: .shared, categories: LiveCategory.allCases.map(\.rawValue))
    @State private var items: [FeedItem] = []
    @State private var trends: [Trend] = []
    @State private var signals: [Signal] = []
    @State private var topic = "all"
    @State private var loading = true
    @State private var error: String?
    @State private var showPersonalize = false
    @State private var livePrefs = LivePrefs.default
    @State private var newItems: [FeedItem] = []      // staged for the "N new" banner
    @State private var lastLoaded = Date()
    @AppStorage("onboarded") private var onboarded = false
    @Environment(\.scenePhase) private var scenePhase
    @Namespace private var zoomNS
    private let refreshTick = Timer.publish(every: 90, on: .main, in: .common).autoconnect()

    /// "All" first, then the user's chosen interests, then everything else.
    private var topics: [String] {
        let all = Set(items.map { $0.topic.lowercased() })
        let mine = (UserDefaults.standard.stringArray(forKey: "user_interests") ?? [])
            .map { $0.lowercased() }.filter { all.contains($0) }
        let rest = all.subtracting(mine).sorted()
        return ["all"] + mine + rest
    }
    private var filtered: [FeedItem] { topic == "all" ? items : items.filter { $0.topic == topic } }
    /// Already-saved preferences, so re-opening "Personalize" edits (not resets) them.
    private var savedContext: UserContext? {
        guard let d = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: d)
    }
    private var greeting: String {
        let h = Calendar.current.component(.hour, from: .now)
        return h < 12 ? "Good morning." : h < 17 ? "Good afternoon." : "Good evening."
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                if loading {
                    ProgressView("Building your lens…").tint(pal.accent)
                } else if let error {
                    ContentUnavailableView {
                        Label("Can't load your brief", systemImage: "wifi.exclamationmark")
                    } description: {
                        Text(error)
                    } actions: {
                        Button("Try again") {
                            loading = true
                            Task { await load() }
                        }
                        .buttonStyle(.borderedProminent).tint(pal.accent)
                    }
                } else {
                    content
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .principal) { EmptyView() } }
            .navigationDestination(for: FeedItem.self) { item in
                StoryDetailView(storyID: item.id)
                    .blZoomDestination(id: item.id, ns: zoomNS)
            }
            .navigationDestination(for: Trend.self) { trend in
                TrendDetailView(trend: trend)
            }
            .navigationDestination(for: Signal.self) { sig in
                SignalDetailView(signal: sig)
            }
            .navigationDestination(for: LiveCard.self) { card in
                StoryDetailView(storyID: card.storyID ?? "")
            }
            .task {
                loadLivePrefs()
                live.start()
                await load()
            }
            .refreshable { await load() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    live.start()
                    // A full reload, not just checkNew(): checkNew() only stages
                    // stories newer than what's on screen, so a story ranked #1
                    // when the app was backgrounded stays #1 forever even after
                    // the backend's recency-decayed score has moved it well down
                    // the list. Resuming from background is an acceptable place
                    // to let the list re-rank and jump scroll — the user is
                    // arriving fresh, not mid-read.
                    Task { await load() }
                } else {
                    live.stop()
                }
            }
            .onReceive(refreshTick) { _ in
                if scenePhase == .active { Task { await checkNew() } }
            }
            // The SSE feed marker flips when new stories land → stage the banner.
            .onChange(of: live.feed?.newestID) { Task { await checkNew() } }
        }
    }

    private var content: some View {
        ScrollView {
            // 12, not 18. Everything above the feed is preamble; on a 390pt-wide
            // phone the old stack pushed the first story card entirely off-screen.
            VStack(alignment: .leading, spacing: 12) {
                LiveHeroView(stream: live, prefs: $livePrefs) {
                    live.reconfigure(categories: livePrefs.categories)
                }
                header
                if !newItems.isEmpty { newStoriesBanner }
                if !onboarded { personalizeBanner }
                if !signals.isEmpty { signalsStrip }
                topicBar
                LazyVStack(spacing: 14) {
                    ForEach(Array(filtered.enumerated()), id: \.element.id) { idx, item in
                        NavigationLink(value: item) {
                            StoryCard(item: item)
                                .blZoomSource(id: item.id, ns: zoomNS)
                        }
                        .buttonStyle(.plain)
                        .scrollTransition(.animated(BL.spring)) { view, phase in
                            view.opacity(phase.isIdentity ? 1 : 0.35)
                                .scaleEffect(phase.isIdentity ? 1 : 0.96)
                                .offset(y: phase.isIdentity ? 0 : 14)
                        }
                        // Explicit dismiss without opening — opening the story
                        // already marks it read (see APIClient.fetchStory).
                        .contextMenu {
                            if api.userID != nil {
                                Button {
                                    Task {
                                        await api.markRead(storyID: item.id)
                                        items.removeAll { $0.id == item.id }
                                    }
                                } label: {
                                    Label("Mark as Read", systemImage: "checkmark.circle")
                                }
                            }
                        }
                    }
                }
                statsCard
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
    }

    /// Masthead. Trimmed to two lines total: the greeting, and one meta line that
    /// absorbed what used to be a chip row plus a separate freshness row. The
    /// streak still has a home in the stats card at the foot of the feed, and the
    /// date is carried by the meta line, so nothing was actually lost.
    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(greeting + " Here's what matters.")
                .font(.system(.title2, design: .serif, weight: .semibold))
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Circle().fill(live.connected ? pal.trust : pal.text2).frame(width: 5, height: 5)
                Text(metaLine)
                    .font(.caption2)
                    .foregroundStyle(pal.text2)
                    .lineLimit(1)
            }
        }
        .padding(.top, 2)
    }

    private var metaLine: String {
        let date = Date.now.formatted(.dateTime.weekday(.abbreviated).month().day())
        let state = live.connected ? "Live" : "Updated \(relative(lastLoaded))"
        return "\(date) · \(filtered.count) stories · ~\(max(2, filtered.count / 2)) min · \(state)"
    }

    private func relative(_ date: Date) -> String {
        let s = Int(Date().timeIntervalSince(date))
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60)m ago" }
        return "\(s / 3600)h ago"
    }

    /// "N new stories" pill — a full reload, so the whole list picks up the
    /// backend's current rank order rather than just prepending the staged
    /// items onto a list that was ranked at some earlier point in time.
    private var newStoriesBanner: some View {
        Button {
            Task { await load() }
        } label: {
            HStack(spacing: 7) {
                Image(systemName: "arrow.up.circle.fill")
                Text("\(newItems.count) new \(newItems.count == 1 ? "story" : "stories")")
                    .font(.footnote.weight(.semibold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16).padding(.vertical, 9)
            .background(Capsule().fill(pal.aiGradient))
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
    }

    /// Opt-in personalization: shown until the user completes "Calibrate your lens".
    private var personalizeBanner: some View {
        Button { showPersonalize = true } label: {
            // One line, not three. It sits between the reader and the news, so it
            // makes its offer and gets out of the way; the full pitch is on the
            // sheet it opens.
            HStack(spacing: 9) {
                Image(systemName: "scope")
                    .font(.caption.weight(.semibold)).foregroundStyle(pal.accent)
                Text("Personalize my news")
                    .font(.footnote.weight(.semibold))
                Text("— what each story means for you")
                    .font(.caption2).foregroundStyle(pal.text2)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold)).foregroundStyle(pal.text2)
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(Capsule()
                .fill(pal.aiGradient.opacity(0.12))
                .overlay(Capsule().stroke(pal.accent.opacity(0.3), lineWidth: 1)))
        }
        .buttonStyle(.plain)
        .sheet(isPresented: $showPersonalize) {
            OnboardingView(initial: savedContext) {
                onboarded = true
                showPersonalize = false
                Task { await load() }
            }
            .environmentObject(api).skinned()
        }
    }

    private var signalsStrip: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("WHAT MAY HAPPEN NEXT")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(pal.prediction)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(signals) { sig in
                        NavigationLink(value: sig) {
                            SignalCard(signal: sig, compact: true)
                        }
                        .buttonStyle(.plain)
                    }
                }
                // The cards decide the strip's height; without this the horizontal
                // ScrollView takes the tallest card and leaves the rest as padding.
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Strips legacy label prefixes from data generated before the prompt fix.
    static func cleanName(_ name: String) -> String {
        var n = name
        for prefix in ["Early signal:", "Early Signal:", "Rising focus:", "Trend:"] {
            if n.lowercased().hasPrefix(prefix.lowercased()) {
                n = String(n.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            }
        }
        return n.isEmpty ? name : n.prefix(1).capitalized + n.dropFirst()
    }

    private var topicBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(topics, id: \.self) { t in
                    Button {
                        withAnimation(BL.spring) { topic = t }
                    } label: {
                        Chip(text: t == "all" ? "All" : t.topicLabel,
                             color: t == topic ? pal.accent : pal.text2,
                             filled: t == topic)
                    }
                }
            }
            .padding(.vertical, 4)
        }
    }

    private var statsCard: some View {
        HStack {
            stat("flame.fill", "\(eng.streak)", "day streak", pal.warning)
            Divider().frame(height: 34).overlay(pal.hairline)
            stat("checkmark.seal.fill", "\(eng.understood)", "completed", pal.trust)
            Divider().frame(height: 34).overlay(pal.hairline)
            stat("safari.fill", "\(eng.topics.count)", "topics", pal.accent)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
        .blCard()
        .padding(.top, 6)
    }

    private func stat(_ icon: String, _ value: String, _ label: String, _ color: Color) -> some View {
        VStack(spacing: 3) {
            Image(systemName: icon).font(.footnote).foregroundStyle(color)
            Text(value).font(.headline.monospaced())
            Text(label).font(.caption2).foregroundStyle(pal.text2)
        }
        .frame(maxWidth: .infinity)
    }

    private func load() async {
        // Two attempts: free-tier servers can take up to a minute to wake
        // from idle, so one failure often just means "still waking up".
        for attempt in 0..<2 {
            do {
                async let f = api.fetchFeed()
                async let t = api.fetchTrends()
                async let g = api.fetchSignals()
                items = try await f
                trends = (try? await t) ?? []
                signals = (try? await g) ?? []
                error = nil
                loading = false
                newItems = []
                lastLoaded = Date()
                return
            } catch {
                if attempt == 0 { try? await Task.sleep(for: .seconds(4)) }
            }
        }
        error = "The server may just be waking up — it naps when idle and takes up to a minute to return. Wait a moment and tap Try again."
        loading = false
    }

    /// Load the hero config from the saved context (falls back to defaults).
    private func loadLivePrefs() {
        if let data = UserDefaults.standard.data(forKey: "saved_context"),
           let ctx = try? JSONDecoder().decode(UserContext.self, from: data),
           let p = ctx.livePrefs {
            livePrefs = p
            live.reconfigure(categories: p.categories)
        }
    }

    /// Fetch only stories newer than what we're showing and stage them for the
    /// banner — never auto-merges, so the reader's scroll position is preserved.
    private func checkNew() async {
        guard !loading, !items.isEmpty else { return }
        let newest = items.compactMap(\.createdAt).max() ?? 0
        guard newest > 0, let fresh = try? await api.fetchFeed(since: newest) else { return }
        let known = Set(items.map(\.id))
        let staged = fresh.filter { !known.contains($0.id) }
        if !staged.isEmpty { withAnimation(BL.spring) { newItems = staged } }
    }
}

// MARK: - Story card

/// Publisher artwork for a story. URL only — nothing is cached to disk by us
/// beyond URLSession's own handling, and the bytes never touch the backend.
///
/// Collapses to zero height when there is no image OR when loading fails:
/// these URLs are hotlinked from publisher CDNs, some of which 404, block
/// hotlinking, or are unreachable on a given network, and an empty grey box
/// in the middle of a card reads as a bug. Text-only is the honest fallback.
struct StoryImage: View {
    @Environment(\.palette) private var pal

    let urlString: String?
    var height: CGFloat = 168
    @State private var failed = false

    var body: some View {
        if let s = urlString, !s.isEmpty, let url = URL(string: s), !failed {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let image):
                    image.resizable().aspectRatio(contentMode: .fill)
                case .failure:
                    // Collapse on the next layout pass rather than showing a gap.
                    Color.clear.onAppear { failed = true }
                case .empty:
                    Rectangle().fill(pal.surface2)
                @unknown default:
                    Color.clear
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: height)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous))
            .accessibilityHidden(true)   // decorative; the headline carries meaning
        }
    }
}

struct StoryCard: View {
    @Environment(\.palette) private var pal

    let item: FeedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            StoryImage(urlString: item.imageUrl, height: 168)
            HStack(spacing: 8) {
                Chip(text: item.topic.topicLabel)
                Spacer()
                ImpactBadge(score: item.impactScore ?? 0)
                LastToldLabel(at: item.createdAt)
            }
            Text(item.headline)
                .font(.system(.title3, design: .serif, weight: .semibold))
                .lineSpacing(1)
                .multilineTextAlignment(.leading)
            Text(item.narrative)
                .font(.subheadline)
                .foregroundStyle(pal.text2)
                .lineLimit(3)
            if let impact = item.impactText, !impact.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.exclamationmark")
                        .font(.caption).foregroundStyle(pal.accent)
                    Text(impact).font(.caption).foregroundStyle(pal.text2).lineLimit(2)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                    .fill(pal.accent.opacity(0.08)))
            }
            TrustMeter(score: item.credibility)
        }
        .padding(18)
        .blCard()
    }
}
