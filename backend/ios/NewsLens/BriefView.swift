import SwiftUI
import Combine

/// The home: a greeting, the orbit — today's stories as single words on rings
/// around the reader, closest first — and, under it, either the stories on the
/// word they tapped or, with nothing tapped, the ordinary ranked list.
///
/// The topic chips pinned under the masthead sit above both. "All" is the
/// orbit and the ranked list; any other topic (Finance, AI…) steps the orbit
/// aside and shows that section's list, as the home did before the orbit.
/// Which ring a story sits on is decided by the server (`GET /orbit`); a guest,
/// or a server that predates the endpoint, gets the list alone.
struct BriefView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    // Live SSE channel for the hero + feed-freshness. Seeded with the shared client
    // (same instance the environment injects) so it needs no environment at init.
    @StateObject private var live = LiveStream(
        api: .shared, categories: LiveCategory.allCases.map(\.rawValue))
    @State private var items: [FeedItem] = []
    /// Nil until loaded, and stays nil for guests and older servers.
    @State private var orbit: Orbit?
    /// The node the reader tapped. Nil = the plain list.
    @State private var selectedNode: String?
    @State private var lensFilter: OrbitLensKind?
    /// The section chip. "all" = the orbit and the whole list.
    @State private var topic = "all"
    /// Personal angles worked out this session (Why me? fetches them), so the
    /// card under the orbit shows the same line the Why-me screen does.
    @State private var impacts: [String: String] = [:]
    @State private var loading = true
    @State private var error: String?
    @State private var showPersonalize = false
    @State private var showAsk = false
    @State private var showProfile = false
    @State private var livePrefs = LivePrefs.default
    @State private var newItems: [FeedItem] = []      // staged for the "N new" banner
    @State private var lastLoaded = Date()
    @AppStorage("onboarded") private var onboarded = false
    @Environment(\.scenePhase) private var scenePhase
    @Namespace private var zoomNS
    private let refreshTick = Timer.publish(every: 90, on: .main, in: .common).autoconnect()

    /// The list under the orbit. A section chip narrows it to that topic; a
    /// lens chip to that lens's orbit stories; otherwise it is the whole ranked
    /// feed.
    private var listItems: [FeedItem] {
        if topic != "all" { return topicItems }
        guard let lensFilter, let orbit else { return items }
        return items.filter { orbit.stories[$0.id]?.lenses.contains(lensFilter.rawValue) == true }
    }

    /// "All" first, then the reader's chosen interests, then everything else.
    ///
    /// Deduplicated on the way out. `user_interests` is free-form storage that
    /// can hold the same interest twice (or "All"), and two identical values in
    /// a `ForEach(id: \.self)` give SwiftUI two views claiming one identity,
    /// which it resolves by drawing one and hit-testing the other.
    private var topics: [String] {
        let all = Set(items.map { $0.topic.lowercased() })
        var seen: Set<String> = ["all"]
        let mine = (UserDefaults.standard.stringArray(forKey: "user_interests") ?? [])
            .map { $0.lowercased() }
            .filter { all.contains($0) && seen.insert($0).inserted }
        let rest = all.subtracting(mine).sorted()
        return ["all"] + mine + rest
    }

    /// Compared lowercased on both sides: one capitalised feed key from the
    /// backend would otherwise silently empty the screen.
    private var topicItems: [FeedItem] {
        let base = items.filter { $0.topic.lowercased() == topic }
        // Local news is only local if it is YOUR city. The `local:` feeds cover
        // several cities, so the reader's own floats to the top and the rest
        // keep the server's order underneath: a stable partition, not a re-sort.
        guard topic == "local", let city = myCity else { return base }
        let mine = base.filter { $0.place?.lowercased() == city }
        return mine + base.filter { $0.place?.lowercased() != city }
    }

    private var myCity: String? {
        let c = (UserContext.saved?.location.city ?? "").trimmingCharacters(in: .whitespaces)
        return c.isEmpty ? nil : c.lowercased()
    }

    /// "Local · Thane" once the reader has told us where they are, "Local"
    /// until then, never a city we only inferred.
    private var localChipLabel: String {
        let c = (UserContext.saved?.location.city ?? "").trimmingCharacters(in: .whitespaces)
        return c.isEmpty ? "Local" : "Local · \(c)"
    }

    /// The orbit is drawn only on "All": it is a map of the whole day, and a
    /// map of one section of it would be mostly empty rings.
    private var showsOrbit: Bool { topic == "all" }

    private var selected: OrbitNode? {
        guard let id = selectedNode else { return nil }
        return orbit?.nodes.first { $0.id == id }
    }

    /// "14 stories · 3 changed recently" — the line under the greeting when
    /// there is no orbit to summarise.
    private var countLine: String {
        let rows = topic == "all" ? items : topicItems
        let n = rows.count
        let changed = rows.filter(\.isDeveloping).count
        var line = "\(n) \(n == 1 ? "story" : "stories")"
        if topic != "all" { line += " in \(topic == "local" ? localChipLabel : topic.topicLabel)" }
        if changed > 0 { line += " · \(changed) changed recently" }
        return line
    }

    /// "THU 24 SEP · LENS: PHARMACY OWNER, PUNE".
    private var dateline: String {
        let date = Date.now.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
        guard let label = orbit?.lens.label, !label.isEmpty else { return date }
        return "\(date) · Lens: \(label)"
    }

    /// "Good morning, Meera." — first name only, and none at all for a guest.
    private var greeting: String {
        let h = Calendar.current.component(.hour, from: .now)
        let part = h < 12 ? "morning" : h < 17 ? "afternoon" : "evening"
        let first = (api.displayName ?? "").split(separator: " ").first.map(String.init)
        return first.map { "Good \(part), \($0)." } ?? "Good \(part)."
    }

    /// "Seven stories reach you today. Two land directly."
    private var summary: String {
        guard showsOrbit, let orbit else { return countLine }
        guard orbit.lens.set else {
            return "\(SpelledCount.of(items.count)) stories today. Tell Descry your world "
                 + "and the ones that touch you move to the centre."
        }
        let n = orbit.stories.count
        let direct = orbit.stories.values.filter { $0.ring == .direct }.count
        let reach = n == 1 ? "One story reaches you today."
                           : "\(SpelledCount.of(n)) stories reach you today."
        let land = direct == 0 ? "None land directly."
                 : direct == 1 ? "One lands directly."
                 : "\(SpelledCount.of(direct)) land directly."
        return "\(reach) \(land)"
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                    masthead
                    if loading {
                        Spacer()
                        ProgressView("Reading this morning's news…").tint(pal.accent)
                        Spacer()
                    } else if let error {
                        Spacer()
                        ContentUnavailableView {
                            Label("Can't load your feed", systemImage: "wifi.exclamationmark")
                        } description: {
                            Text(error)
                        } actions: {
                            Button("Try again") {
                                loading = true
                                Task { await load() }
                            }
                            .buttonStyle(.borderedProminent).tint(pal.accent)
                        }
                        Spacer()
                    } else {
                        pinnedTopicBar
                        content
                    }
                }
            }
            // Hidden, but titled: the title is what a pushed screen's back
            // button reads, and the Why-me screen's says "Orbit".
            .navigationTitle("Orbit")
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showAsk) {
                AskAISheet(story: nil).environmentObject(api).skinned()
            }
            // A sheet rather than a push: ProfileView owns a NavigationStack of
            // its own, and nesting one inside this one buries the back button
            // under the inner stack's bar.
            .sheet(isPresented: $showProfile) {
                ProfileView().environmentObject(api).environmentObject(ThemeStore.shared).skinned()
            }
            .sheet(isPresented: $showPersonalize) {
                OnboardingView(initial: UserContext.saved) {
                    onboarded = true
                    showPersonalize = false
                    Task { await load() }
                }
                .environmentObject(api).skinned()
            }
            .navigationDestination(for: FeedItem.self) { item in
                // Finance-pipeline stories share this feed with ordinary
                // coverage, so the tap has to pick the reader that can actually
                // render what the card promised: FinanceStoryView draws the
                // metrics table and per-actor sentiment that StoryDetailView has
                // no fields for.
                Group {
                    if item.isFinance {
                        FinanceStoryView(storyID: item.id)
                    } else {
                        StoryDetailView(storyID: item.id)
                    }
                }
                .blZoomDestination(id: item.id, ns: zoomNS)
            }
            .navigationDestination(for: WhyRoute.self) { route in
                WhyMeView(route: route,
                          impact: impacts[route.item.id] ?? route.item.impactText,
                          onImpact: { impacts[route.item.id] = $0 },
                          onNotRelevant: {
                              selectedNode = nil
                              Task { await load() }
                          },
                          onLensChanged: { Task { await load() } })
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
                    // to let the list re-rank — the user is arriving fresh.
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
            // A lens chip that hides the tapped node also closes its panel —
            // otherwise the panel would describe a node drawn as disabled.
            // Leaving "All" puts the orbit away, so nothing tapped on it
            // should still be in force when the reader comes back.
            .onChange(of: topic) {
                selectedNode = nil
                lensFilter = nil
            }
            .onChange(of: lensFilter) { _, lens in
                if let lens, let node = selected, !node.lenses.contains(lens.rawValue) {
                    selectedNode = nil
                }
            }
        }
    }

    private var content: some View {
      ScrollViewReader { proxy in
        ScrollView {
            // Nothing above the orbit changes height after the first paint:
            // the summary reserves its two lines, and the live strip that
            // materialises late lives BELOW the orbit, in the list. A node that
            // moves under the reader's thumb opens the wrong story.
            VStack(alignment: .leading, spacing: 0) {
                Color.clear.frame(height: 0).id(Self.feedTop)
                header
                if showsOrbit, let orbit, !orbit.nodes.isEmpty || !orbit.lens.set {
                    OrbitLensChips(orbit: orbit, lens: $lensFilter)
                        .padding(.top, 16)
                    OrbitCanvas(orbit: orbit, selected: $selectedNode, lens: lensFilter) {
                        if orbit.lens.set {
                            withAnimation(BL.spring) {
                                selectedNode = nil
                                lensFilter = nil
                            }
                        } else {
                            showPersonalize = true
                        }
                    }
                    .padding(.top, 8)
                }
                if showsOrbit, let node = selected, let orbit {
                    OrbitPanel(node: node,
                               items: node.storyIDs.compactMap { id in items.first { $0.id == id } },
                               orbit: orbit, impacts: impacts) {
                        withAnimation(BL.spring) { selectedNode = nil }
                    }
                    .padding(.top, 10)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                } else {
                    list.padding(.top, showsOrbit && orbit != nil ? 4 : 12)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 6)
            .padding(.bottom, selectedNode == nil ? 40 : 0)
        }
        .scrollIndicators(.hidden)
        // The chips stay reachable from anywhere in the feed, so changing one
        // has to return you to the top. Otherwise the list re-renders above
        // you and it looks like nothing happened.
        .onChange(of: topic) {
            withAnimation(BL.spring) { proxy.scrollTo(Self.feedTop, anchor: .top) }
        }
      }
    }

    private static let feedTop = "feed-top"

    /// The section filter, pinned under the masthead, deliberately OUTSIDE the
    /// feed's scroll view, for two reasons:
    ///
    /// 1. Views above a chip row that appear late (the live strip, the "N new"
    ///    banner) push it down under the reader's thumb, and the tap lands on
    ///    whatever slid into its place.
    /// 2. A tap that stops a decelerating scroll view is consumed by it, which
    ///    reads exactly like "the chip didn't register".
    ///
    /// Out here neither can happen, and it stays reachable while scrolled.
    private var pinnedTopicBar: some View {
        // One topic is not a filter: with only "All" the bar is dead chrome.
        Group {
            if topics.count > 1 {
                topicBar
                    .padding(.vertical, 9)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(pal.hairline).frame(height: 1)
                    }
            }
        }
    }

    private var topicBar: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 7) {
                    ForEach(topics, id: \.self) { t in
                        Button {
                            withAnimation(BL.spring) { topic = t }
                        } label: {
                            // Outline, filling with ink when on. The orbit's
                            // lens chips are the filled grey kind, so the two
                            // rows never read as one control.
                            Chip(text: t == "all" ? "All"
                                     : t == "local" ? localChipLabel
                                     : t.topicLabel,
                                 color: pal.text, filled: t == topic)
                        }
                        .buttonStyle(.plain)
                        .id(t)
                        .accessibilityAddTraits(t == topic ? [.isSelected] : [])
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 2)
            }
            // A chip near the right edge would otherwise be selected out of
            // sight, and the filter would look like it had done nothing.
            .onChange(of: topic) { _, t in
                withAnimation(BL.spring) { proxy.scrollTo(t, anchor: .center) }
            }
        }
    }

    /// Today's list, unchanged in form: the live strip, the lead story at full
    /// weight, then rule-separated rows.
    @ViewBuilder
    private var list: some View {
        VStack(alignment: .leading, spacing: 12) {
            LiveHeroView(stream: live, prefs: $livePrefs) {
                live.reconfigure(categories: livePrefs.categories)
            }
            if !newItems.isEmpty { newStoriesBanner }
            if !onboarded { personalizeBanner }
            let rows = listItems
            if let lead = rows.first {
                link(lead) { HeroStory(item: lead).blZoomSource(id: lead.id, ns: zoomNS) }
            }
            if rows.count > 1 {
                listHead
                LazyVStack(spacing: 0) {
                    ForEach(rows.dropFirst()) { item in
                        link(item) { StoryRow(item: item) }
                    }
                }
            }
            statsCard
        }
    }

    /// One story's tap target, with the dismiss action every list entry carries.
    private func link<V: View>(_ item: FeedItem, @ViewBuilder _ label: () -> V) -> some View {
        NavigationLink(value: item) { label() }
            .buttonStyle(.plain)
            // Explicit dismiss without opening — opening the story already marks
            // it read (see APIClient.fetchStory).
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

    /// The fixed masthead: the wordmark, a menu (Ask, the lens, the account),
    /// and the reader's initial. Profile has no tab — five is already the most
    /// a 390pt bar can label — so the account lives here, as on the web.
    private var masthead: some View {
        HStack(spacing: 16) {
            Text("Descry")
                .font(pal.serif(27, .medium))
                .foregroundStyle(pal.text)
                .accessibilityAddTraits(.isHeader)
            Spacer()
            Menu {
                Button { showAsk = true } label: {
                    Label("Ask about today's news", systemImage: "sparkle")
                }
                Button { showPersonalize = true } label: {
                    Label("Edit your lens", systemImage: "scope")
                }
                Button { showProfile = true } label: {
                    Label("Account & appearance", systemImage: "person.crop.circle")
                }
            } label: {
                Image(systemName: "line.3.horizontal")
                    .font(.system(size: 18, weight: .regular))
                    .foregroundStyle(pal.text2)
                    .frame(width: 36, height: 36)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Menu")
            Button { showProfile = true } label: { avatar }
                .buttonStyle(.plain)
                .accessibilityLabel("Your account")
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var avatar: some View {
        let initial = (api.displayName ?? "").trimmingCharacters(in: .whitespaces).prefix(1)
        ZStack {
            Circle().fill(pal.sandEdge)
            if initial.isEmpty {
                Image(systemName: "person.fill")
                    .font(.system(size: 15))
                    .foregroundStyle(pal.skin == .signal ? pal.ink2 : .hex(0x17150F))
            } else {
                Text(initial.uppercased())
                    .font(pal.serif(19).italic())
                    .foregroundStyle(pal.skin == .signal ? pal.ink2 : .hex(0x17150F))
            }
        }
        .frame(width: 36, height: 36)
    }

    /// Dateline, greeting, and the one-sentence summary of the orbit.
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(dateline)
                .font(pal.mono(11.5, .medium))
                .kerning(1.4)
                .textCase(.uppercase)
                .foregroundStyle(pal.faint)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Text(greeting)
                .font(pal.serif(31))
                .foregroundStyle(pal.text)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            HStack(alignment: .top, spacing: 6) {
                if live.connected {
                    Circle().fill(pal.goodFill).frame(width: 5, height: 5).padding(.top, 8)
                }
                Text(summary)
                    .font(pal.sans(15.5))
                    .lineSpacing(3)
                    .foregroundStyle(pal.text2)
                    // Reserves both lines up front, so the chips and orbit
                    // below never move when the summary grows or shrinks.
                    .lineLimit(2, reservesSpace: true)
            }
        }
        .padding(.top, 6)
    }

    /// The hairline-and-label rule that separates the lead story from the list.
    private var listHead: some View {
        HStack(spacing: 12) {
            Text(topic != "all" ? "More in \(topic == "local" ? "Local" : topic.topicLabel)"
                 : lensFilter.map { "More in \($0.label)" } ?? "Also today")
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(pal.faint)
            Rectangle().fill(pal.hairline).frame(height: 1)
        }
        .padding(.top, 10)
    }

    /// "N new stories" pill — a full reload, so the whole list picks up the
    /// backend's current rank order rather than just prepending the staged
    /// items onto a list that was ranked at some earlier point in time.
    private var newStoriesBanner: some View {
        Button {
            Task { await load() }
        } label: {
            HStack(spacing: 7) {
                Image(systemName: "arrow.up").font(.system(size: 11, weight: .semibold))
                Text("\(newItems.count) new \(newItems.count == 1 ? "story" : "stories")")
                    .font(pal.sans(13.5, .medium))
            }
            .foregroundStyle(pal.ink)
            .padding(.horizontal, 16).padding(.vertical, 9)
            .background(Capsule().fill(pal.text))
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
            HStack(spacing: 0) {
                Rectangle().fill(pal.sandEdge).frame(width: 2)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Tell Descry your world")
                        .font(pal.serif(16, .medium))
                        .foregroundStyle(pal.sandInk)
                    Text("Once — then the stories that touch you move to the centre of your orbit.")
                        .font(pal.sans(14))
                        .lineSpacing(4)
                        .foregroundStyle(pal.sandText)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 13).padding(.vertical, 11)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(pal.sandInk)
                    .padding(.trailing, 12)
            }
            .background(pal.sand)
            .clipShape(UnevenRoundedRectangle(bottomTrailingRadius: pal.r(5),
                                              topTrailingRadius: pal.r(5)))
        }
        .buttonStyle(.plain)
    }

    // The forecasts strip that used to sit here is gone: the design gives
    // forecasts a tab of their own ("What's Next"), the same move the web
    // portal made when Trends stopped rendering forecast cards and started
    // pointing at /next. Two homes for one thing is how they drift apart.

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

    /// The foot of the feed. Set as a line of type rather than three icons in a
    /// panel: it is a note about your reading, not a scoreboard, and the design
    /// has no badges anywhere.
    private var statsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(pal.hairline).frame(height: 1)
            Text("\(eng.understood) \(eng.understood == 1 ? "story" : "stories") read through · "
                 + "\(eng.topics.count) \(eng.topics.count == 1 ? "topic" : "topics") · "
                 + "day \(eng.streak)")
                .font(pal.mono(12.5))
                .foregroundStyle(pal.faint)
                .padding(.top, 14)
        }
        .padding(.top, 18)
    }

    private func load() async {
        // Two attempts: free-tier servers can take up to a minute to wake
        // from idle, so one failure often just means "still waking up".
        for attempt in 0..<2 {
            do {
                // The feed and the orbit arrive together (GET /orbit wraps
                // /feed), so the orbit costs no second request.
                let home = try await api.fetchHome()
                items = home.items
                OrbitHomeCache.shared.items = home.items
                withAnimation(BL.spring) {
                    orbit = home.orbit
                    // A reload can drop the tapped node (read, dismissed, or
                    // aged out); keep the selection only if it survived.
                    if let id = selectedNode, home.orbit?.nodes.contains(where: { $0.id == id }) != true {
                        selectedNode = nil
                    }
                    // Same for the section chip: a topic with nothing left in
                    // it has no chip, and an empty list under no chip is a
                    // dead end.
                    if topic != "all", !home.items.contains(where: { $0.topic.lowercased() == topic }) {
                        topic = "all"
                    }
                }
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
        if let p = UserContext.saved?.livePrefs {
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
//
// `StoryImage` moved to ImageLoader.swift, where it decodes at the size it is
// drawn instead of handing a 1024px publisher JPEG to the main thread for an
// 84pt thumbnail.

// MARK: - The kicker
//
// "Developing · still unfolding", or "Business · 3 min read · updated 2h ago".
// Rust when the story is still moving, quiet when it is settled — the colour is
// carrying a fact, not decoration.

struct StoryKicker: View {
    let item: FeedItem
    @Environment(\.palette) private var pal

    var body: some View {
        Group {
            if item.isDeveloping {
                Text("Developing · still unfolding")
                    .foregroundStyle(pal.breaking)
            } else {
                Text("\(item.sectionLabel) · \(item.readingMinutes) min read"
                     + (item.updatedAt ?? item.createdAt != nil
                        ? " · \(Ago.short(item.updatedAt ?? item.createdAt))" : ""))
                    .foregroundStyle(pal.faint)
            }
        }
        .font(pal.mono(12, .medium))
        .kerning(1.68)
        .textCase(.uppercase)
        .lineLimit(1)
        .minimumScaleFactor(0.8)
    }
}

// MARK: - "Why this matters to you"

/// The sand panel with the rule down its left edge. Only drawn when there is a
/// real personalized line — the empty state is an invitation elsewhere, not a
/// panel with nothing in it.
struct WhyThisMatters: View {
    let text: String
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 0) {
            Rectangle().fill(pal.sandEdge).frame(width: 2)
            VStack(alignment: .leading, spacing: 7) {
                Text("Why this matters to you")
                    .font(pal.serif(17, .medium))
                    .foregroundStyle(pal.sandInk)
                Text(text)
                    .font(pal.sans(14.5))
                    .lineSpacing(5)
                    .foregroundStyle(pal.sandText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 13).padding(.vertical, 11)
            Spacer(minLength: 0)
        }
        .background(pal.sand)
        .clipShape(UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: 0,
                                          bottomTrailingRadius: pal.r(5),
                                          topTrailingRadius: pal.r(5)))
    }
}

// MARK: - The lead story

struct HeroStory: View {
    @Environment(\.palette) private var pal

    let item: FeedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            StoryImage(urlString: item.imageUrl, height: 150, squareTop: true)
            VStack(alignment: .leading, spacing: 0) {
                StoryKicker(item: item).padding(.bottom, 8)
                Text(item.headline)
                    .font(pal.serif(22))
                    .lineSpacing(2)
                    .foregroundStyle(pal.text)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 8)
                // The evidence strip: how many sources agree, and how much of
                // the story has actually been checked. Ruled top and bottom, so
                // it reads as a measurement rather than more headline.
                VStack(spacing: 0) {
                    Rectangle().fill(pal.hairline).frame(height: 1)
                    // Two lines, not one. The mockup gives the facts count
                    // `flex-basis:100%` — it wraps beneath the verdict — and at
                    // the design's sizes there is no width on a 390pt phone for
                    // both on one line without truncating the sentence that has
                    // to be read in full.
                    VStack(alignment: .leading, spacing: 4) {
                        AgreementLine(credibility: item.credibility)
                        if let facts = item.factsCheckedLine {
                            Text(facts)
                                .font(pal.mono(12.5))
                                .foregroundStyle(pal.mute)
                                .lineLimit(1)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 9)
                    Rectangle().fill(pal.hairline).frame(height: 1)
                }
                .padding(.bottom, 11)
                if let c = item.correction { CorrectionNote(correction: c) }
                if let impact = item.impactText, !impact.isEmpty {
                    WhyThisMatters(text: impact)
                }
            }
            .padding(.horizontal, 15)
            .padding(.top, 14)
            .padding(.bottom, 16)
        }
        .background(pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
            .stroke(pal.hairline2, lineWidth: 1))
    }
}

// MARK: - A list row

/// Everything after the lead: the verdict, the headline, and a flag only when
/// there is something true to flag. No card, no image — separated by a rule.
struct StoryRow: View {
    @Environment(\.palette) private var pal

    let item: FeedItem

    var body: some View {
        // Text column then an 84pt square, exactly as 6a specifies. Only the
        // lead story gets a full-width photograph: a second one would make the
        // list read as two leads, and the thumbnail keeps the agreement line
        // and headline first in the reading order.
        HStack(alignment: .top, spacing: 13) {
            VStack(alignment: .leading, spacing: 6) {
                // The verdict gets the line to itself. Sharing it with the row
                // flag squeezed both into "Nearly all s… / only one source say…",
                // which is the one sentence on the row that has to be readable.
                AgreementLine(credibility: item.credibility, size: 12.5, pipWidth: 10)
                Text(item.headline)
                    .font(pal.serif(17))
                    .lineSpacing(1.5)
                    .foregroundStyle(pal.text)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                metaLine
                if let c = item.correction { CorrectionNote(correction: c) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            // Self-collapsing: a story with no artwork loses the square rather
            // than reserving an empty one, so the text simply runs full width.
            StoryImage(urlString: item.imageUrl, height: 84, width: 84)
        }
        .padding(.vertical, 15)
        .overlay(alignment: .top) { Rectangle().fill(pal.hairline).frame(height: 1) }
        .contentShape(Rectangle())
    }

    /// "4 sources · 2h ago", with a rust clause appended only when there is
    /// something true to warn about.
    ///
    /// The old row flag "only one source says this" is gone: this line already
    /// says "1 source", and printing both was the same fact twice, in the space
    /// the verdict needed.
    @ViewBuilder
    private var metaLine: some View {
        let facts: [String] = {
            var parts: [String] = []
            if let n = item.sourceCount, n > 0 {
                parts.append("\(n) source\(n == 1 ? "" : "s")")
            }
            if let at = item.updatedAt ?? item.createdAt, at > 0 {
                parts.append(Ago.short(at))
            }
            return parts
        }()
        let warn: String? = {
            if let d = item.claimsDisputed, d > 0 {
                return "\(d) fact\(d == 1 ? "" : "s") argued over"
            }
            return nil
        }()
        if !facts.isEmpty || warn != nil {
            // Stacked rather than joined: at the design's 12.5px the text
            // column of a thumbnail row is not wide enough for both clauses on
            // one line, and the warning is the half that must not be clipped.
            VStack(alignment: .leading, spacing: 3) {
                if !facts.isEmpty {
                    Text(facts.joined(separator: " · "))
                        .font(pal.mono(12.5))
                        .foregroundStyle(pal.faint)
                        .lineLimit(1)
                }
                if let warn {
                    Text(warn)
                        .font(pal.mono(12.5))
                        .foregroundStyle(pal.breaking)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// The general-purpose story card, used by every list that is not the feed —
/// Saved, Read, a trend's stories, a forecast's stories. The feed itself uses
/// `HeroStory` + `StoryRow`; this is the same vocabulary (kicker, serif
/// headline, agreement line) in a self-contained card, so those screens read as
/// part of the same paper even before they get their own layouts.
struct StoryCard: View {
    @Environment(\.palette) private var pal

    let item: FeedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            StoryImage(urlString: item.imageUrl, height: 140)
            HStack(spacing: 8) {
                StoryKicker(item: item)
                Spacer(minLength: 0)
                FinanceBadge(item: item)
                ImpactBadge(score: item.impactScore ?? 0)
            }
            Text(item.headline)
                .font(pal.serif(19))
                .lineSpacing(1.5)
                .foregroundStyle(pal.text)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            Text(item.narrative)
                .font(pal.sans(14.5))
                .lineSpacing(5)
                .foregroundStyle(pal.text3)
                .lineLimit(3)
            AgreementLine(credibility: item.credibility, size: 12.5, pipWidth: 10)
            if let c = item.correction { CorrectionNote(correction: c) }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
            .stroke(pal.hairline2, lineWidth: 1))
    }
}

/// "✦ FINANCE SPECIAL · 6 FIGURES" — marks a story written by the finance
/// pipeline rather than the news one.
///
/// Both pipelines read the same articles, so a finance beat carries both kinds
/// of telling and they sit side by side under the same topic chip. Nothing else
/// on the card distinguishes them, and the difference is the whole point: this
/// one has a verbatim-anchored metrics table and per-actor sentiment behind it.
/// The figure count is included because "Finance special" is a label, while
/// "6 figures" is a reason to open it.
///
/// Draws nothing at all for an ordinary story, so every existing card is
/// unchanged — and nothing is drawn on a finance story from a server too old to
/// send `kind`, which is the safe direction.
struct FinanceBadge: View {
    let item: FeedItem
    @Environment(\.palette) private var pal

    var body: some View {
        if item.isFinance {
            HStack(spacing: 3) {
                Image(systemName: "sparkle").font(.system(size: 8.5))
                Text(figures.isEmpty ? "FINANCE" : "FINANCE · \(figures)")
                    .font(pal.mono(10, .semibold))
                    .kerning(1.1)
            }
            .foregroundStyle(pal.accent)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Capsule().fill(pal.accent.opacity(0.13)))
            .overlay(Capsule().stroke(pal.accent.opacity(0.32), lineWidth: 1))
            .lineLimit(1)
        }
    }

    private var figures: String {
        guard let n = item.metricCount, n > 0 else { return "" }
        return "\(n) FIG"
    }
}

/// "Fewer sources agree now · 72 → 31". Drawn only when the server actually
/// sent a correction, which it does only for stories whose corroboration
/// really fell. Nothing is drawn for a story that is fine.
struct CorrectionNote: View {
    let correction: Correction
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 6) {
            Rectangle().fill(pal.badFill).frame(width: 12, height: 1)
            Text(correction.heading)
                .font(pal.mono(12, .medium))
                .foregroundStyle(pal.breaking)
            if let note = correction.note, !note.isEmpty {
                Text(note).font(pal.sans(12.5)).foregroundStyle(pal.mute).lineLimit(2)
            } else if let from = correction.from, let to = correction.to {
                Text("\(Int(from.rounded())) → \(Int(to.rounded()))")
                    .font(pal.mono(12)).foregroundStyle(pal.mute)
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 2)
    }
}
