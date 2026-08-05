import SwiftUI

/// "What's Next" — the forecasts tab (mockups 5b and 7b).
///
/// One long scroll: the warning, the filters, the lead forecast opened out with
/// its reasoning shown inline, the rest as cards, the far-out ones in their own
/// section, then the track record and how these are made.
///
/// **What 7b asks for that is not drawn here, and why.** Each of these needs a
/// record we have never kept, and inventing any of them would make this the one
/// thing the design says a forecasting page must never be — a page that scores
/// itself:
///
///   • **"3 of 4 checkpoints met".** A checkpoint is a dated thing we predicted
///     and then confirmed. Nothing writes those. The thread below shows the
///     stories the forecast was built FROM, which is a different claim, so it
///     is labelled "how we got here" and never counted as checkpoints met.
///   • **"raised from 'could go either way' on 3 Aug".** There is no history
///     table for a forecast's confidence — only its current value.
///   • **"19 of 26 came true" and "Settled · 26".** Forecasts are hard-deleted
///     seven days after their last update (`DELETE FROM signals` in
///     Foresight.run), so there is no archive to count. This is the blocker
///     worth fixing first: every day that passes destroys the evidence a track
///     record would be built from.
///   • **"Tell me when" switches.** There is no push infrastructure, and a
///     switch that cannot fire is worse than no switch.
struct NextView: View {
    @Environment(\.palette) private var pal
    @EnvironmentObject var api: APIClient

    @State private var signals: [Signal] = []
    /// Derived ONCE per load, not per render.
    ///
    /// These were computed properties, and `deduped` walked all ~200 forecasts
    /// building a dictionary and a Set every time it was read. `mine`, `changed`,
    /// `pool`, `near`, `distant` and `countLine` each read it, so a single pass
    /// over `body` ran the dedupe six times — and `body` re-runs whenever the
    /// SSE stream, a bookmark or the scene phase changes. `Lens.touches` was
    /// lower-casing every forecast's text on each of those passes too.
    @State private var deduped: [Signal] = []
    @State private var mine: [Signal] = []
    @State private var loading = true
    @State private var error: String?
    @State private var showAsk = false

    enum Cut: String { case mine, all, soonest, likely }
    @State private var cut: Cut = .all

    /// Titles are collapsed to one card. Successive pipeline runs can write the
    /// same forecast again with a different horizon (8 of the 200 rows the
    /// server currently returns are repeats), and three identical headlines in
    /// a row look like a rendering bug. The strongest of each set survives.
    ///
    /// The server's order is then restored. Sorting by confidence to pick the
    /// survivor used to leave the whole list sorted that way, which threw away
    /// the recency half of the server's rank — a nine-day-old forecast at 0.9
    /// outranked a fresh one at 0.8 — and made the "Most likely first" pill a
    /// duplicate of "All".
    private static func collapse(_ signals: [Signal]) -> [Signal] {
        var best: [String: Signal] = [:]
        for s in signals {
            let key = s.title.lowercased()
            if let cur = best[key], cur.confidence >= s.confidence { continue }
            best[key] = s
        }
        let keep = Set(best.values.map(\.id))
        return signals.filter { keep.contains($0.id) }
    }
    /// Written in the last day. NOT "in the last week": forecasts are deleted
    /// after seven days, so a seven-day window counts the whole list and the
    /// header read "192 open · 189 new".
    @State private var changed: [Signal] = []

    private var pool: [Signal] { cut == .mine ? mine : deduped }
    private var near: [Signal] {
        let list = pool.filter { !Horizon.isDistant($0.horizon) }
        switch cut {
        case .soonest: return list.sorted { ($0.horizon ?? "zz") < ($1.horizon ?? "zz") }
        case .likely:  return list.sorted { $0.confidence > $1.confidence }
        default:       return list
        }
    }
    private var distant: [Signal] { pool.filter { Horizon.isDistant($0.horizon) } }

    /// The design's page is a considered handful; the server currently returns
    /// ~190 distinct forecasts. Showing all of them puts the closing sections —
    /// the track record and how these are made, the two things that tell the
    /// reader how much to trust the page — an unreachable distance below the
    /// fold. Nothing is hidden: the real count is on the button.
    private static let pageSize = 12
    @State private var expanded = false
    private var shownNear: [Signal] {
        expanded ? near : Array(near.prefix(Self.pageSize))
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                    header
                    if loading {
                        Spacer(); ProgressView().tint(pal.accent); Spacer()
                    } else if let error {
                        Spacer()
                        ContentUnavailableView("Can't load forecasts",
                                               systemImage: "wifi.exclamationmark",
                                               description: Text(error))
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
            .navigationDestination(for: Signal.self) { SignalDetailView(signal: $0) }
            .navigationDestination(for: FeedItem.self) { StoryDetailView(storyID: $0.id) }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private var header: some View {
        PageHeader(title: "What's Next", subtitle: countLine) {
            Button { showAsk = true } label: {
                Image(systemName: "sparkle")
                    .font(.system(size: 15))
                    .foregroundStyle(pal.text2)
                    .frame(width: 30, height: 30)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Ask about what's next")
        }
    }

    private var countLine: String {
        guard !deduped.isEmpty else { return loading ? "reading…" : "none open" }
        var line = "\(deduped.count) open"
        if !changed.isEmpty { line += " · \(changed.count) new" }
        return line
    }

    @ViewBuilder
    private var list: some View {
        if deduped.isEmpty {
            Spacer()
            ContentUnavailableView("Nothing forecast yet", systemImage: "clock",
                description: Text("Forecasts appear once several stories point the same way."))
            Spacer()
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    intro
                    readThisFirst
                    pills
                    // LazyVStack, not a bare ForEach in the VStack. The server
                    // returns ~190 distinct forecasts; building every card
                    // eagerly — each with its own thread, bars and pills — hung
                    // the screen long enough to time out a screenshot.
                    if !near.isEmpty {
                        sectionRule("Within a year")
                        LazyVStack(spacing: 12) {
                            ForEach(Array(shownNear.enumerated()), id: \.element.id) { idx, sig in
                                NavigationLink(value: sig) {
                                    ForecastCard(signal: sig, lead: idx == 0,
                                                 mine: Lens.touches(sig.lensText))
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(.bottom, 12)
                        if near.count > Self.pageSize, !expanded {
                            Button { withAnimation(BL.spring) { expanded = true } } label: {
                                Text("Show all \(near.count) forecasts")
                                    .font(pal.sans(14))
                                    .foregroundStyle(pal.accent)
                                    .padding(.vertical, 12)
                                    .frame(maxWidth: .infinity)
                                    .overlay(RoundedRectangle(cornerRadius: pal.r(8))
                                        .stroke(pal.hairline2, lineWidth: 1))
                            }
                            .buttonStyle(.plain)
                            .padding(.bottom, 12)
                        }
                    }
                    if !distant.isEmpty {
                        sectionRule("Further out")
                        LazyVStack(spacing: 10) {
                            ForEach(distant) { sig in
                                NavigationLink(value: sig) {
                                    DistantForecastCard(signal: sig)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(.bottom, 10)
                    }
                    if near.isEmpty && distant.isEmpty {
                        Text("Nothing matches that filter.")
                            .font(pal.sans(14))
                            .foregroundStyle(pal.mute)
                            .padding(.vertical, 24)
                    }
                    scorecard
                    howWeMakeThese
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 30)
            }
            .scrollIndicators(.hidden)
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Where things seem to be going — and how sure we are.")
                .font(pal.serif(26, .light))
                .lineSpacing(3)
                .kerning(-0.4)
                .foregroundStyle(pal.text)
                .fixedSize(horizontal: false, vertical: true)
            Text("Nothing here has happened yet. Each one is our reading of stories we already checked.")
                .font(pal.sans(15))
                .lineSpacing(6)
                .foregroundStyle(pal.text3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 14)
    }

    private var readThisFirst: some View {
        HStack(spacing: 0) {
            Rectangle().fill(pal.sandEdge).frame(width: 2)
            VStack(alignment: .leading, spacing: 6) {
                Text("Read this first")
                    .font(pal.serif(16, .medium))
                    .foregroundStyle(pal.sandInk)
                Text("A forecast is not news. It is thinking out loud, with the working shown. Trust it less than the Feed — and hold us to it.")
                    .font(pal.sans(14))
                    .lineSpacing(5)
                    .foregroundStyle(pal.sandText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 15).padding(.vertical, 13)
            Spacer(minLength: 0)
        }
        .background(pal.sand)
        .clipShape(UnevenRoundedRectangle(bottomTrailingRadius: pal.r(6),
                                          topTrailingRadius: pal.r(6)))
        .padding(.bottom, 18)
    }

    /// Bleeds to the screen edges by widening the scroll view, not by
    /// `scrollClipDisabled()` — see the note on `BriefView.topicBar`: a pill
    /// drawn outside the scroll view's bounds is not tappable, and the tap
    /// lands on whatever is behind it.
    private var pills: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 7) {
                    if !mine.isEmpty { pill(.mine, "Matters to you", mine.count) }
                    pill(.all, "All", deduped.count)
                    pill(.soonest, "Soonest", nil)
                    pill(.likely, "Most likely first", nil)
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

    /// "What we said before, and what happened".
    ///
    /// The design's single most trust-building block, and the one we cannot
    /// fill. This draws the section, states exactly why it is empty and what
    /// would fill it, and prints no number — a made-up hit rate would undo
    /// precisely the trust the block exists to build.
    private var scorecard: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("What we said before, and what happened")
                .font(pal.serif(20, .medium))
                .foregroundStyle(pal.text)
                .fixedSize(horizontal: false, vertical: true)
            Text("Nothing yet — and that is a gap in what we have built, not a good result.")
                .font(pal.sans(14.5))
                .lineSpacing(5)
                .foregroundStyle(pal.text2)
                .fixedSize(horizontal: false, vertical: true)
            Text("A forecast is deleted a week after it stops moving, so there is no archive to go back to and no way to count what came true. Until forecasts are kept past their date and marked against what actually happened, this section stays empty rather than showing you a score we made up.")
                .font(pal.sans(13))
                .lineSpacing(5)
                .foregroundStyle(pal.text3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.surface2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous))
        .padding(.top, 12)
    }

    private var howWeMakeThese: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("How we make these")
                .font(pal.serif(17, .medium))
                .foregroundStyle(pal.text)
            Text("Only from stories we have already checked — never from how loud something is online. Every forecast names the stories behind it and one thing you can go and check for yourself. We date it, and we leave it up whether it holds or not.")
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
        do {
            let fresh = try await api.fetchSignals()
            let collapsed = Self.collapse(fresh)
            let dayAgo = Date().timeIntervalSince1970 - 86_400
            signals = fresh
            deduped = collapsed
            mine = collapsed.filter { Lens.touches($0.lensText) }
            changed = collapsed.filter { ($0.createdAt ?? 0) > dayAgo }
            error = nil
        } catch {
            self.error = "The server may be waking up. Pull to try again."
        }
        loading = false
    }
}

// MARK: - One forecast

/// The lead card is inverted and opened out — reasoning, likelihood and the
/// thing that would stop it, all on the list page. The rest are on paper and
/// carry only what a scan needs.
struct ForecastCard: View {
    let signal: Signal
    var lead: Bool
    var mine: Bool
    @Environment(\.palette) private var pal

    /// The inverted card fixes its own colours: the same night ground the
    /// "Read for you" page and the forecast detail use, and for the same
    /// reason — it marks writing that is ours rather than reported.
    private var ink: Color { .hex(0x17150F) }
    private var paper: Color { .hex(0xF7F4EE) }
    private var gold: Color { .hex(0xC8B98F) }

    private var when: String {
        Horizon.byWhen(signal.horizon, from: signal.createdAt)
            ?? signal.horizon.flatMap { $0.isEmpty ? nil : $0 }
            ?? "no date given"
    }
    private var mix: SourceMix { SourceMix(signal.stories) }
    private var hasFalsifier: Bool { signal.falsifier?.isEmpty == false }
    /// Newest first, capped — forecasts here cite as many as 39 stories.
    private var chain: [FeedItem] {
        Array(signal.stories.sorted { ($0.createdAt ?? 0) > ($1.createdAt ?? 0) }.prefix(3))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            kicker
            Text(signal.title)
                .font(pal.serif(lead ? 26 : 20, lead ? .light : .regular))
                .lineSpacing(lead ? 3 : 2)
                .kerning(lead ? -0.4 : 0)
                .foregroundStyle(lead ? paper : pal.text)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, lead ? 12 : 9)
            Text(signal.prediction)
                .font(pal.sans(lead ? 15 : 14.5))
                .lineSpacing(lead ? 6.5 : 6)
                .foregroundStyle(lead ? paper.opacity(0.72) : pal.text3)
                .multilineTextAlignment(.leading)
                .lineLimit(lead ? nil : 3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, lead ? 18 : 12)

            if lead { leadBody }
            footer
        }
        .padding(.horizontal, lead ? 19 : 18)
        .padding(.top, lead ? 20 : 17)
        .padding(.bottom, lead ? 20 : 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(lead ? ink : pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(lead ? 11 : 10), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(lead ? 11 : 10), style: .continuous)
            .stroke(lead ? .clear : pal.hairline2, lineWidth: 1))
    }

    /// The reasoning, shown on the list rather than hidden behind a tap — the
    /// thread is what makes a forecast feel like an argument instead of a
    /// prediction, so scrolling it *is* walking the reasoning.
    @ViewBuilder
    private var leadBody: some View {
        if signal.isLocked {
            Text("The stories behind this, and what would change our mind, are behind sign-in.")
                .font(pal.sans(14))
                .lineSpacing(5)
                .foregroundStyle(paper.opacity(0.72))
                .fixedSize(horizontal: false, vertical: true)
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(gold.opacity(0.3), lineWidth: 1))
                .padding(.bottom, 14)
        } else {
            // Deliberately "How we got here" and not "3 of 4 checkpoints met".
            // These are the stories the forecast was built FROM. A checkpoint
            // is something we predicted and later confirmed, and nothing
            // records those — see the note on NextView.
            Text("How we got here")
                .font(pal.serif(17, .medium))
                .foregroundStyle(paper)
                .padding(.bottom, 13)
            ForEach(chain) { s in node(s) }
            HStack(alignment: .top, spacing: 12) {
                Circle().strokeBorder(gold, style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                    .frame(width: 9, height: 9)
                    .padding(.top, 5)
                    .frame(width: 16)
                Text("Where it goes next")
                    .font(pal.sans(15))
                    .foregroundStyle(gold)
                Spacer(minLength: 0)
            }
            .padding(.bottom, 20)

            sureness
            if signal.falsifier?.isEmpty == false || !signal.watch.isEmpty { toWatch }
        }
    }

    private func node(_ s: FeedItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                Circle().fill(gold).frame(width: 8, height: 8).padding(.top, 5)
                Rectangle().fill(gold.opacity(0.45)).frame(width: 1).frame(maxHeight: .infinity)
            }
            .frame(width: 16)
            VStack(alignment: .leading, spacing: 3) {
                Text(s.headline)
                    .font(pal.sans(15))
                    .lineSpacing(3)
                    .foregroundStyle(paper)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Text(nodeMeta(s))
                    .font(pal.mono(12))
                    .foregroundStyle(paper.opacity(0.6))
            }
            Spacer(minLength: 0)
        }
        .padding(.bottom, 14)
    }

    private func nodeMeta(_ s: FeedItem) -> String {
        var parts = [AgreementBand.sentence(s.credibility).lowercased()]
        if let at = s.createdAt, at > 0 {
            parts.append(Date(timeIntervalSince1970: at)
                .formatted(.dateTime.day().month(.abbreviated)))
        }
        return parts.joined(separator: " · ")
    }

    /// The confidence, as a bar. The mockup adds "raised from 'could go either
    /// way' on 3 Aug"; nothing records a forecast's previous confidence, so the
    /// change line is absent rather than invented.
    private var sureness: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("How sure are we?")
                .font(pal.serif(15, .medium))
                .foregroundStyle(paper)
            SegmentBar(lit: max(1, Int((signal.confidence * 5).rounded())),
                       color: gold, track: paper.opacity(0.2), height: 7)
            Text("\(Odds.word(signal.confidence).lowercased()) — how far the stories below point the same way, not a record of how often we are right")
                .font(pal.mono(13))
                .lineSpacing(3)
                .foregroundStyle(paper.opacity(0.62))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(paper.opacity(0.14), lineWidth: 1))
        .padding(.bottom, 11)
    }

    /// The mockup titles this "What would stop it" — a disproof. For a long time
    /// we had nothing to put under that heading: `watch` is the opposite, a
    /// confirming indicator, and printing it there would have inverted what the
    /// model wrote. The prompt now asks for a real falsifier as a separate
    /// field, so the heading follows the data: a forecast that has one says what
    /// would stop it, and one that does not still shows what to watch.
    private var toWatch: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(hasFalsifier ? "What would stop it" : "What to watch")
                .font(pal.serif(15, .medium))
                .foregroundStyle(paper)
            Text(hasFalsifier ? signal.falsifier! : signal.watch)
                .font(pal.sans(14))
                .lineSpacing(5)
                .foregroundStyle(paper.opacity(0.72))
                .fixedSize(horizontal: false, vertical: true)
            if hasFalsifier, !signal.watch.isEmpty {
                Text("On track if: \(signal.watch)")
                    .font(pal.sans(13))
                    .lineSpacing(4)
                    .foregroundStyle(paper.opacity(0.55))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(paper.opacity(0.14), lineWidth: 1))
        .padding(.bottom, 16)
    }

    /// Wrapping, not a rigid row: the mockup's kicker is `flex-wrap:wrap`, and
    /// at the design's sizes a date plus "Could go either way" plus "matters to
    /// you" is wider than a 390pt phone.
    private var kicker: some View {
        BLFlow(spacing: 7, lineSpacing: 7) {
            Text(when)
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(lead ? gold : pal.faint)
                .fixedSize()
            pill(Odds.word(signal.confidence),
                 fg: lead ? gold : Odds.color(signal.confidence, pal),
                 bg: lead ? gold.opacity(0.18) : pal.surface2)
            if mine {
                pill(lead ? "matters to you" : "near you",
                     fg: lead ? paper.opacity(0.82) : pal.sandInk,
                     bg: lead ? paper.opacity(0.1) : pal.sand)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, 12)
    }

    private func pill(_ text: String, fg: Color, bg: Color) -> some View {
        Text(text)
            .font(pal.sans(12.5, .medium))
            .foregroundStyle(fg)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(Capsule().fill(bg))
            .lineLimit(1)
    }

    /// The counts the card closes on. "Built on 2 trends" is not among them:
    /// a forecast row records the stories it was built from and never the
    /// trends, so the trend count is not something we can state.
    private var footer: some View {
        HStack(spacing: 12) {
            let n = signal.storyCount ?? signal.stories.count
            if n > 0 {
                chipText("\(n) \(n == 1 ? "story" : "stories")",
                         tint: lead ? paper.opacity(0.82) : pal.faint)
            }
            if signal.isLocked {
                chipText("sign in to read them", tint: lead ? paper.opacity(0.82) : pal.faint)
            } else if let warn = mix.interestedWarning {
                // Real, and worth the space: it is the one thing on the card
                // that argues against believing it.
                chipText(warn, tint: lead ? gold : pal.warning)
            }
            Spacer(minLength: 0)
        }
        .padding(.top, lead ? 15 : 12)
        .overlay(alignment: .top) {
            Rectangle().fill(lead ? paper.opacity(0.14) : pal.hairline).frame(height: 1)
        }
    }

    private func chipText(_ s: String, tint: Color) -> some View {
        Text(s)
            .font(pal.mono(13))
            .foregroundStyle(tint)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// A forecast measured in years. Lighter treatment: the claim, what it stands
/// on, and how likely — no reasoning, because at this range the reasoning is
/// the least reliable part.
struct DistantForecastCard: View {
    let signal: Signal
    @Environment(\.palette) private var pal

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(signal.title)
                .font(pal.serif(17.5))
                .lineSpacing(2)
                .foregroundStyle(pal.text)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 5)
            Text(meta)
                .font(pal.mono(12.5))
                .foregroundStyle(pal.faint)
                .padding(.bottom, 10)
            HStack(spacing: 10) {
                SegmentBar(lit: max(1, Int((signal.confidence * 5).rounded())),
                           color: Odds.color(signal.confidence, pal),
                           track: pal.hairline2, height: 6)
                Text(Odds.word(signal.confidence))
                    .font(pal.sans(13, .medium))
                    .foregroundStyle(Odds.color(signal.confidence, pal))
                    .fixedSize()
            }
        }
        .padding(.horizontal, 17)
        .padding(.vertical, 15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.ink2)
        .clipShape(RoundedRectangle(cornerRadius: pal.r(9), style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.r(9), style: .continuous)
            .stroke(pal.hairline2, lineWidth: 1))
    }

    private var meta: String {
        var parts: [String] = []
        if let h = signal.horizon, !h.isEmpty { parts.append(h) }
        let n = signal.storyCount ?? signal.stories.count
        if n > 0 { parts.append("\(n) \(n == 1 ? "story" : "stories")") }
        if let warn = SourceMix(signal.stories).interestedWarning { parts.append(warn) }
        return parts.joined(separator: " · ")
    }
}
