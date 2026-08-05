import SwiftUI

/// Trends as a stack (mockup 3b) — what keeps coming back, weighed by how well
/// it holds up rather than by how loud it is.
///
/// The "What may happen" half this screen used to carry is gone: forecasts have
/// their own tab now, and a screen that also listed them meant two places to
/// keep in step. Trends is about what has already happened, repeatedly.
struct TrendsView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @State private var trends: [Trend] = []
    @State private var loading = true
    @State private var showAsk = false
    @Environment(\.scenePhase) private var scenePhase

    /// Which cut of the list is showing. These are re-orderings and filters of
    /// what is already loaded — no extra request, and each pill only appears
    /// when it would actually return something.
    enum Cut: String, CaseIterable { case lens, all, strongest, fading, newest }
    @State private var cut: Cut = .all

    private var all: [Trend] { trends.filter { $0.kind == "macro" } }
    private var mine: [Trend] { all.filter { Lens.touches($0.lensText) } }
    private var faded: [Trend] { all.filter { TrendStatus.of($0).kind == .fading } }
    /// Trends holding stories whose own corroboration has since fallen.
    ///
    /// NOT `updated_at` within a week, which was the first attempt: every trend
    /// is re-synthesised on every pipeline run, so that read "40 running · 40
    /// changed this week" — a count of the list's own length dressed up as
    /// news. `weakened_count` is the only genuine "this moved" signal the
    /// server sends, and it is only present when it is non-zero.
    private var weakened: [Trend] {
        all.filter { ($0.weakenedCount ?? 0) > 0 }
    }

    /// The list as shown. "Strongest" and "Newest" re-rank; "Your lens" and
    /// "Fading" filter. Default order is the server's ranking, which already
    /// blends recency with how much is behind each trend.
    private var shown: [Trend] {
        switch cut {
        case .all:       return all
        case .lens:      return mine
        case .fading:    return faded
        case .strongest: return all.sorted { TrendStrength.of($0).lit > TrendStrength.of($1).lit }
        case .newest:    return all.sorted { ($0.createdAt ?? 0) > ($1.createdAt ?? 0) }
        }
    }

    /// Fading trends are kept on the page rather than dropped, so they sit at
    /// the end of the default list instead of interleaved with live ones.
    private var ordered: [Trend] {
        guard cut == .all else { return shown }
        return shown.filter { TrendStatus.of($0).kind != .fading } + faded
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                    header
                    if loading {
                        Spacer(); ProgressView().tint(pal.accent); Spacer()
                    } else if all.isEmpty {
                        Spacer()
                        ContentUnavailableView("No trends yet",
                            systemImage: "chart.line.uptrend.xyaxis",
                            description: Text("A trend appears once several stories keep returning to the same thing."))
                        Spacer()
                    } else {
                        list
                    }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showAsk) {
                AskAISheet(story: nil).environmentObject(api).skinned()
            }
            .navigationDestination(for: Trend.self) { TrendDetailView(trend: $0) }
            .navigationDestination(for: Signal.self) { SignalDetailView(signal: $0) }
            .navigationDestination(for: FeedItem.self) { StoryDetailView(storyID: $0.id) }
            .task { await load() }
            .refreshable { await load() }
            // Like BriefView, this view lives inside a persistent TabView, so
            // .task fires once per app launch and never again — without this,
            // trends ranked on day one stay pinned at the top no matter how
            // long the app stays backgrounded and resumed.
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await load() } }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("Trends")
                    .font(pal.serif(24))
                    .foregroundStyle(pal.text)
                Text(countLine)
                    .font(pal.mono(13))
                    .foregroundStyle(pal.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
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
            .accessibilityLabel("Ask about these trends")
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
        .padding(.bottom, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(pal.hairline).frame(height: 1) }
    }

    /// "9 running · 4 match your lens · 2 changed this week". Each clause is
    /// only added when it is non-zero — a "0 match your lens" reads as a
    /// verdict on the trends rather than on an empty lens.
    private var countLine: String {
        guard !all.isEmpty else { return loading ? "counting…" : "none running" }
        var parts = ["\(all.count) running"]
        if !mine.isEmpty { parts.append("\(mine.count) match your lens") }
        if !weakened.isEmpty { parts.append("\(weakened.count) weakened") }
        return parts.joined(separator: " · ")
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("A trend is a pattern we can count — never how loud something is online. Each one shows the stories behind it and how much its sources argue.")
                    .font(pal.sans(15.5))
                    .lineSpacing(6)
                    .foregroundStyle(pal.text2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 14)
                pills
                // The label names what the current cut actually is. It said
                // "Holding strongest" over the default (server-ranked) order,
                // which put a one-day-old trend under a claim about strength.
                sectionRule(cutLabel)
                LazyVStack(spacing: 14) {
                    ForEach(Array(ordered.enumerated()), id: \.element.id) { idx, t in
                        NavigationLink(value: t) { TrendCard(trend: t, rank: idx) }
                            .buttonStyle(.plain)
                    }
                }
                if ordered.isEmpty {
                    Text("Nothing matches that filter.")
                        .font(pal.sans(14))
                        .foregroundStyle(pal.mute)
                        .padding(.vertical, 24)
                }
                whereTheseLead
                howWeCount
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
            .padding(.bottom, 30)
        }
        .scrollIndicators(.hidden)
    }

    /// Bleeds to the screen edges by widening the scroll view, not by
    /// `scrollClipDisabled()` — see the note on `BriefView.topicBar`: a pill
    /// drawn outside the scroll view's bounds is not tappable, and the tap
    /// lands on whatever is behind it.
    private var pills: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 7) {
                    if !mine.isEmpty { pill(.lens, "Your lens", mine.count) }
                    pill(.all, "All", all.count)
                    pill(.strongest, "Strongest", nil)
                    if !faded.isEmpty { pill(.fading, "Fading", faded.count) }
                    pill(.newest, "Newest first", nil)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 2)
            }
            .padding(.horizontal, -20)
            .onChange(of: cut) { _, c in
                withAnimation(BL.spring) { proxy.scrollTo(c, anchor: .center) }
            }
        }
        .padding(.bottom, 18)
    }

    private func pill(_ kind: Cut, _ label: String, _ count: Int?) -> some View {
        Button { withAnimation(BL.spring) { cut = kind } } label: {
            Text(count.map { "\(label) · \($0)" } ?? label)
                .font(pal.sans(13.5))
                .foregroundStyle(cut == kind ? pal.ink : pal.text2)
                .padding(.horizontal, 13).padding(.vertical, 7)
                .background(Capsule().fill(cut == kind ? pal.text : .clear))
                .overlay(Capsule().stroke(cut == kind ? pal.text : pal.hairline2, lineWidth: 1))
                .lineLimit(1)
        }
        .buttonStyle(.plain)
        .id(kind)
        .accessibilityAddTraits(cut == kind ? [.isSelected] : [])
    }

    private var cutLabel: String {
        switch cut {
        case .all:       return "Running now"
        case .lens:      return "Matching your lens"
        case .strongest: return "Holding strongest"
        case .fading:    return "Kept on the page"
        case .newest:    return "Newest first"
        }
    }

    private func sectionRule(_ label: String) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(pal.faint)
                .fixedSize()
            Rectangle().fill(pal.hairline2).frame(height: 1)
        }
        .padding(.bottom, 12)
    }

    /// The pointer to the forecasts tab. Deliberately does NOT claim which
    /// trends feed which forecast: a forecast row records the stories it was
    /// built from, never the trends, so "two of these trends point at the same
    /// forecast" is not something we can currently say.
    private var whereTheseLead: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Where these point")
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(Color.hex(0xC8B98F))
                .padding(.bottom, 9)
            Text("What these patterns might turn into is kept on its own page.")
                .font(.system(size: 18, design: .serif))
                .foregroundStyle(Color.hex(0xF7F4EE))
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 6)
            Text("A guess never borrows the authority of a count, so forecasts do not appear on this screen.")
                .font(pal.sans(14))
                .lineSpacing(5)
                .foregroundStyle(Color.hex(0xF7F4EE).opacity(0.7))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.hex(0x17150F))
        .clipShape(RoundedRectangle(cornerRadius: pal.r(9), style: .continuous))
        .padding(.top, 20)
    }

    private var howWeCount: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("How we count a trend")
                .font(pal.serif(16.5, .medium))
                .foregroundStyle(pal.text)
            Text("Only stories we have already checked are counted, and a trend's strength is its story count, its number of outlets and how far those outlets agree — never how much attention it is getting. When its sources start arguing, the card says so instead of quietly dropping it.")
                .font(pal.sans(13.5))
                .lineSpacing(5)
                .foregroundStyle(pal.text3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(9), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(9), style: .continuous)
            .stroke(pal.hairline2, lineWidth: 1))
        .padding(.top, 12)
    }

    private func load() async {
        trends = (try? await api.fetchTrends()) ?? []
        loading = false
    }
}

// MARK: - Trend deep-dive

struct TrendDetailView: View {
    @Environment(\.palette) private var pal

    let trend: Trend
    @EnvironmentObject var api: APIClient
    @State private var detail: TrendDetail?
    @State private var openStoryID: String?

    var body: some View {
        ZStack {
            InkBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(spacing: 8) {
                        if retired {
                            Chip(text: "Past trend")
                        } else {
                            Chip(text: trend.kind == "micro" ? "Picking up speed" : "Big trend",
                                 color: trend.kind == "micro" ? pal.prediction : pal.accent,
                                 filled: true)
                        }
                        LastToldChip(at: detail?.createdAt ?? trend.createdAt)
                        Spacer()
                        if !retired {
                            Sparkline(seed: trend.name,
                                      color: trend.kind == "micro" ? pal.prediction : pal.accent,
                                      width: 90, height: 26)
                        }
                    }
                    Text(BriefView.cleanName(trend.name))
                        .font(.system(.title, design: .serif, weight: .semibold))
                        .lineLimit(4)
                        .minimumScaleFactor(0.8)
                    if retired { retiredBanner }
                    LinkedText(text: detail?.narrative ?? trend.narrative,
                               refs: detail?.storyRefs ?? [])
                        .font(.subheadline).foregroundStyle(pal.text2)
                    if let sectors = detail?.sectors ?? trend.sectors, !sectors.isEmpty {
                        HStack(spacing: 8) {
                            ForEach(sectors.prefix(4), id: \.self) { Chip(text: $0) }
                        }
                    }
                    Divider().overlay(pal.hairline)
                    Text("THE STORIES BEHIND THIS TREND")
                        .font(.caption2.weight(.bold)).kerning(1)
                        .foregroundStyle(pal.text2)
                    if let stories = detail?.stories {
                        if stories.isEmpty {
                            Text("No stories linked yet — the next pipeline run will connect them.")
                                .font(.footnote).foregroundStyle(pal.text2)
                        } else {
                            LazyVStack(spacing: 12) {
                                ForEach(stories) { s in
                                    NavigationLink(value: s) { StoryCard(item: s) }
                                        .buttonStyle(.plain)
                                }
                            }
                        }
                    } else {
                        ProgressView().tint(pal.accent)
                            .frame(maxWidth: .infinity).padding(.top, 20)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.top, 8)
                .padding(.bottom, 40)
            }
            .scrollIndicators(.hidden)
        }
        .onStoryLink { openStoryID = $0 }
        .navigationDestination(item: $openStoryID) { StoryDetailView(storyID: $0) }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                ShareLink(item: api.shareURL("trend/\(trend.id)"),
                          subject: Text(trend.name),
                          message: Text("\(BriefView.cleanName(trend.name)) — via Descry")) {
                    Image(systemName: "square.and.arrow.up")
                        .foregroundStyle(pal.accent)
                }
                .accessibilityLabel("Share this trend")
            }
        }
        .task { detail = try? await api.fetchTrendDetail(id: trend.id) }
    }

    /// Only the detail response knows about retirement — the list never carries
    /// retired trends, so a retired one is always reached by a direct link.
    private var retired: Bool { detail?.isRetired ?? false }

    private var retiredBanner: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "archivebox")
                .font(.footnote).foregroundStyle(pal.warning)
            VStack(alignment: .leading, spacing: 3) {
                Text("No longer an active trend.")
                    .font(.footnote.weight(.semibold))
                Text("Descry stopped tracking this\(LastTold.relative(detail?.retiredAt).map { " \($0)" } ?? "") because recent coverage moved on. Everything below is kept as it was reported at the time.")
                    .font(.caption).foregroundStyle(pal.text2)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
            .fill(pal.warning.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
            .stroke(pal.warning.opacity(0.3), lineWidth: 1))
    }
}

/// One trend in the stack: where it is in its life, what it says, how much
/// there is to lean on, and the counts behind that.
///
/// There is no sparkline any more. It was drawn from a hash of the trend's
/// name — a deterministic scribble that looked exactly like a measurement and
/// was not one. The strength bar replaces it and is computed from real counts.
struct TrendCard: View {
    @Environment(\.palette) private var pal

    let trend: Trend
    /// Position in the stack; only the top few are set large.
    var rank: Int = 0

    private var status: TrendStatus { .of(trend) }
    private var strength: TrendStrength { .of(trend) }
    private var headlineSize: CGFloat { rank == 0 ? 22 : rank == 1 ? 20 : 18.5 }

    private func tone(_ t: AgreementBand.Tone) -> Color {
        switch t {
        case .good: return pal.trust
        case .mid:  return pal.warning
        case .bad:  return pal.breaking
        }
    }
    private func fill(_ t: AgreementBand.Tone) -> Color {
        switch t {
        case .good: return pal.goodFill
        case .mid:  return pal.midFill
        case .bad:  return pal.badFill
        }
    }

    private var isFading: Bool { status.kind == .fading }
    private var inLens: Bool { Lens.touches(trend.lensText) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // "Strengthening · 6 weeks" — a state and an age, both measured.
            // Wrapping, as the mockup's own `flex-wrap:wrap` does: at 12px with
            // .14em tracking, "SOURCES DISAGREE · 4 WEEKS" plus the lens pill is
            // wider than the card on a 390pt phone.
            BLFlow(spacing: 8, lineSpacing: 7) {
                Text("\(status.word) · \(status.note)")
                    .font(pal.mono(12, .medium))
                    .kerning(1.68)
                    .textCase(.uppercase)
                    .foregroundStyle(status.kind == .strong ? pal.breaking : tone(status.tone))
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                if inLens {
                    Text("your lens")
                        .font(pal.mono(12))
                        .foregroundStyle(pal.sandInk)
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(Capsule().fill(pal.sand))
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 9)
            Text(BriefView.cleanName(trend.name))
                .font(pal.serif(headlineSize))
                .lineSpacing(1.5)
                .foregroundStyle(pal.text)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, isFading ? 8 : 10)

            // A fading trend is kept on the page but set quietly: no argument,
            // just why it is still here. Dropping it silently is what the design
            // says not to do.
            if isFading {
                Text(fadingNote)
                    .font(pal.mono(13))
                    .foregroundStyle(pal.faint)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                // Capped: trend narratives run to a dozen lines and the card is
                // a summary with a table under it. The full text is on the
                // trend's own page, one tap away.
                Text(trend.narrative)
                    .font(pal.sans(rank == 0 ? 15 : 14.5))
                    .lineSpacing(6)
                    .foregroundStyle(rank == 0 ? pal.text2 : pal.text3)
                    .multilineTextAlignment(.leading)
                    .lineLimit(rank == 0 ? 5 : 4)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 14)
                HStack(spacing: 10) {
                    SegmentBar(lit: strength.lit, color: fill(strength.tone),
                               track: pal.hairline2, height: 9)
                    Text(strength.word)
                        .font(pal.sans(rank == 0 ? 14.5 : 14, .medium))
                        .foregroundStyle(tone(strength.tone))
                        .fixedSize()
                }
                .padding(.vertical, 12)
                .overlay(alignment: .top) { Rectangle().fill(pal.hairline).frame(height: 1) }
                .overlay(alignment: .bottom) { Rectangle().fill(pal.hairline).frame(height: 1) }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(strength.word). \(strength.why)")
                receipts
                Text("See all \(trend.storyCount ?? trend.articleCount ?? 0) stories →")
                    .font(pal.sans(14))
                    .foregroundStyle(pal.accent)
                    .padding(.top, 13)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 17)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
            .stroke(pal.hairline2, lineWidth: 1))
        .opacity(isFading ? 0.74 : 1)
    }

    /// The receipts table — the argument itself, one labelled row per figure.
    ///
    /// The mockup has four rows. Two of them we cannot fill and so do not draw:
    ///   • "Independent sources · 11 original documents" — `kinds.primary` in
    ///     sources.yaml is empty, so this is 0 for every trend in the
    ///     catalogue. See `SourceMix`.
    ///   • "Direction, 6 weeks" — needs a weekly story count going back six
    ///     weeks. `/trends` aggregates over a 7-day window only, so there is no
    ///     series to plot; inventing one from `velocity` would be a shape, not
    ///     a measurement.
    private var receipts: some View {
        VStack(spacing: 0) {
            if let n = trend.storyCount, n > 0 {
                row("Stories behind it", "\(n)")
            } else if let a = trend.articleCount, a > 0 {
                row("Articles behind it", "\(a)", note: "none written up this week")
            }
            if let a = trend.agree, let d = trend.disagree, a + d > 0 {
                row("Agree / disagree", "\(a) / \(d)",
                    tint: d > 0 && Double(d) >= Double(a) * 0.5 ? pal.warning : nil, last: trend.weakenedCount == nil)
            }
            if let w = trend.weakenedCount, w > 0 {
                row("Since weakened", "\(w)", tint: pal.breaking, last: true)
            }
        }
    }

    private func row(_ label: String, _ value: String,
                     note: String? = nil, tint: Color? = nil, last: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(pal.sans(14))
                .foregroundStyle(pal.text2)
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(value)
                    .font(pal.mono(14))
                    .foregroundStyle(tint ?? pal.text)
                if let note {
                    Text(note)
                        .font(pal.mono(12))
                        .foregroundStyle(pal.faint)
                }
            }
        }
        .padding(.vertical, 10)
        .overlay(alignment: .bottom) {
            if !last { Rectangle().fill(pal.hairline).frame(height: 1) }
        }
    }

    private var fadingNote: String {
        let n = trend.articleCount ?? 0
        return n > 0
            ? "\(n) article\(n == 1 ? "" : "s"), none written up this week · kept visible on purpose"
            : "no new stories this week · kept visible on purpose"
    }

}
