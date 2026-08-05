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
    @State private var topic = "all"
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

    /// "All" first, then the user's chosen interests, then everything else.
    ///
    /// Deduplicated on the way out. `user_interests` is free-form storage that
    /// can hold the same interest twice (or "All"), and two identical values in
    /// a `ForEach(id: \.self)` give SwiftUI two views claiming one identity —
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
    /// Compared lowercased on both sides: the chip values are lower-cased when
    /// the list is built, so comparing them against a raw `topic` matches only
    /// as long as the backend keeps sending lower-case categories. It does
    /// today; one capitalised feed key would silently empty the screen.
    private var filtered: [FeedItem] {
        topic == "all" ? items : items.filter { $0.topic.lowercased() == topic }
    }
    /// Already-saved preferences, so re-opening "Personalize" edits (not resets) them.
    private var savedContext: UserContext? {
        guard let d = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: d)
    }
    /// "Monday morning" — the mockup's masthead. A weekday and a part of the
    /// day, not "Good morning": the page is titled with when you are reading it,
    /// which is what makes the count line beneath it mean something.
    private var greeting: String {
        let h = Calendar.current.component(.hour, from: .now)
        let part = h < 12 ? "morning" : h < 17 ? "afternoon" : "evening"
        return "\(Date.now.formatted(.dateTime.weekday(.wide))) \(part)"
    }

    /// "14 stories · 3 changed overnight". The second half is only printed when
    /// stories really did move — `updated_at` pulling away from `created_at` is
    /// the Storyteller retelling a developing event, so this is a counted fact
    /// rather than a flourish, and it disappears on a quiet day.
    private var countLine: String {
        let n = filtered.count
        let changed = filtered.filter(\.isDeveloping).count
        var line = "\(n) \(n == 1 ? "story" : "stories")"
        if changed > 0 { line += " · \(changed) changed recently" }
        return line
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
      ScrollViewReader { proxy in
        ScrollView {
            // 12, not 18. Everything above the feed is preamble; on a 390pt-wide
            // phone the old stack pushed the first story card entirely off-screen.
            VStack(alignment: .leading, spacing: 12) {
                // A zero-height anchor rather than an `.id` on the live strip:
                // the strip draws no view at all until its first SSE card
                // arrives, and an anchor that comes and goes is not an anchor.
                Color.clear.frame(height: 0).id(Self.feedTop)
                LiveHeroView(stream: live, prefs: $livePrefs) {
                    live.reconfigure(categories: livePrefs.categories)
                }
                header
                if !newItems.isEmpty { newStoriesBanner }
                if !onboarded { personalizeBanner }
                // The lead story is drawn at full weight and everything after it
                // as a rule-separated row. That is the mockup's whole feed
                // structure: one story you are meant to read, then a list you are
                // meant to scan — not fifteen identical cards competing.
                if let lead = filtered.first {
                    link(lead) { HeroStory(item: lead).blZoomSource(id: lead.id, ns: zoomNS) }
                }
                if filtered.count > 1 {
                    listHead
                    LazyVStack(spacing: 0) {
                        ForEach(filtered.dropFirst()) { item in
                            link(item) { StoryRow(item: item) }
                        }
                    }
                }
                statsCard
            }
            .padding(.horizontal, 20)
            .padding(.top, 6)      // clear of the pinned filter bar's rule
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        // The filter is reachable from anywhere in the feed now, so changing it
        // has to return you to the top. Without this you tap a chip 800pt down
        // and the list re-renders above you — which looks like nothing happened.
        .onChange(of: topic) {
            withAnimation(BL.spring) { proxy.scrollTo(Self.feedTop, anchor: .top) }
        }
      }
    }

    /// Scroll anchor for the head of the feed.
    private static let feedTop = "feed-top"

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

    /// The fixed masthead: wordmark, and the two things reachable from anywhere
    /// — Ask, and the account. The design's phone bar carries the wordmark and
    /// Ask; the account button is here because the five tabs it draws leave no
    /// room for a Profile tab, and an app you cannot sign into is not a design
    /// improvement.
    private var masthead: some View {
        HStack {
            DescryLockup()
            Spacer()
            Button { showAsk = true } label: {
                HStack(spacing: 5) {
                    Image(systemName: "sparkle").font(.system(size: 11))
                    Text("Ask").font(pal.sans(13))
                }
                .foregroundStyle(pal.text2)
                .padding(.horizontal, 11).padding(.vertical, 5)
                .overlay(Capsule().stroke(pal.hairline2, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Ask about today's news")
            Button { showProfile = true } label: {
                Image(systemName: "person.crop.circle")
                    .font(.system(size: 18))
                    .foregroundStyle(pal.text2)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Your account")
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
        .padding(.bottom, 12)
        .overlay(alignment: .bottom) { Rectangle().fill(pal.hairline).frame(height: 1) }
    }

    /// "Monday morning" over "14 stories · 3 changed recently".
    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(greeting)
                .font(pal.serif(22))
                .foregroundStyle(pal.text)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                if live.connected {
                    Circle().fill(pal.goodFill).frame(width: 5, height: 5)
                }
                Text(countLine)
                    .font(pal.mono(13))
                    .foregroundStyle(pal.mute)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
        }
        .padding(.top, 6)
    }

    /// The hairline-and-label rule that separates the lead story from the list.
    private var listHead: some View {
        HStack(spacing: 12) {
            Text(topic == "all" ? "Also today" : "More in \(topic.topicLabel)")
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
                    Text("Once — then every story says what it means for you.")
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
        .sheet(isPresented: $showPersonalize) {
            OnboardingView(initial: savedContext) {
                onboarded = true
                showPersonalize = false
                Task { await load() }
            }
            .environmentObject(api).skinned()
        }
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

    /// The topic filter, pinned under the masthead — deliberately OUTSIDE the
    /// feed's scroll view.
    ///
    /// It used to sit in the scrolling column, and that placement was the whole
    /// of "the chips work sometimes, and when they don't the hero story opens
    /// instead". Two separate reasons, both structural:
    ///
    /// 1. **Everything above it can appear from nothing.** `LiveHeroView` draws
    ///    no view at all until its first SSE card lands, and that card arrives
    ///    after the feed has painted — so a ~120pt strip materialises under your
    ///    thumb a beat after the screen looks settled, and the chip row drops by
    ///    that much. `newStoriesBanner` (90s timer) and `personalizeBanner` do
    ///    the same. What slides into the space the chip just left is the lead
    ///    story's `NavigationLink`, so the tap opens a story. Giving the live
    ///    card a fixed height fixed its 18pt internal wobble but not this.
    /// 2. **A tap that stops a decelerating scroll view is consumed by it.**
    ///    Flick the feed, reach for a chip before it settles, and the first tap
    ///    only halts the scroll. That is standard iOS behaviour and it reads
    ///    exactly like "the chip didn't register".
    ///
    /// Out here neither can happen: nothing above it changes size, and it is not
    /// inside the scroller that eats the tap. It also stays reachable while the
    /// feed is scrolled, which is what a filter is for.
    private var pinnedTopicBar: some View {
        // One topic is not a filter — with only "All" the bar is dead chrome.
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
                            // Solid ink when on, outline when off — see Chip.
                            // The selected chip takes the ink colour rather
                            // than the accent so the filter never competes
                            // with a link.
                            Chip(text: t == "all" ? "All" : t.topicLabel,
                                 color: pal.text, filled: t == topic)
                        }
                        .buttonStyle(.plain)
                        .id(t)
                        .accessibilityAddTraits(t == topic ? [.isSelected] : [])
                    }
                }
                // The page inset lives on the content, so the row itself runs
                // edge to edge and chips scroll out under the screen edge
                // rather than stopping short of it.
                .padding(.horizontal, 20)
                .padding(.vertical, 2)
            }
            // Tapping a chip near the right edge used to leave the selection
            // scrolled out of sight, so the filter looked like it had done
            // nothing.
            .onChange(of: topic) { _, t in
                withAnimation(BL.spring) { proxy.scrollTo(t, anchor: .center) }
            }
        }
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
                // Only the feed now. Trends and forecasts have their own tabs
                // and fetch their own data; asking for all three here paid for
                // two requests per refresh that the screen never drew.
                items = try await api.fetchFeed()
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
                Text("\(item.topic.topicLabel) · \(item.readingMinutes) min read"
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
