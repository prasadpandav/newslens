import SwiftUI

// MARK: - Signal card (used in the Brief strip and the Radar list)

struct SignalCard: View {
    @Environment(\.palette) private var pal

    let signal: Signal
    var compact = false

    var body: some View {
        Group { compact ? AnyView(compactBody) : AnyView(fullBody) }
            .padding(compact ? 11 : 16)
            .frame(width: compact ? 214 : nil, alignment: .leading)
            .frame(maxWidth: compact ? nil : .infinity, alignment: .leading)
            .blCard(radius: compact ? 12 : 18)
    }

    /// How many stories this forecast stands on.
    ///
    /// `stories` is withheld from signed-out callers — the reasoning is the
    /// thing worth an account — but `story_count` is still sent, precisely so
    /// the card can say what is behind the forecast without having received it.
    /// Counting the withheld array instead printed "based on 0 stories" on
    /// every card for every guest, which reads as "we made this up".
    private var storyCount: Int { signal.storyCount ?? signal.stories.count }

    private var basisLine: String {
        storyCount == 0 ? "no stories linked yet"
            : "from \(storyCount) \(storyCount == 1 ? "story" : "stories")"
            + (signal.isLocked ? " · sign in to read them" : "")
    }

    /// The Brief's horizontal peek. Deliberately two lines of headline and one
    /// meta row — this strip sits above the feed, so every point it spends is a
    /// point of actual news the reader has to scroll past it to reach.
    private var compactBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                // The word, not the percentage — see `Odds`.
                Text(Odds.word(signal.confidence))
                    .font(pal.sans(12.5, .medium))
                    .foregroundStyle(Odds.color(signal.confidence, pal))
                Spacer(minLength: 4)
                Text("\(storyCount) stories")
                    .font(.caption2)
                    .foregroundStyle(pal.text2)
            }
            Text(signal.title)
                .font(.footnote.weight(.semibold))
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var fullBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(Odds.word(signal.confidence))
                    .font(pal.sans(12.5, .medium))
                    .foregroundStyle(Odds.color(signal.confidence, pal))
                Spacer()
                if let h = signal.horizon, !h.isEmpty {
                    Text(h)
                        .font(pal.mono(12.5))
                        .foregroundStyle(pal.faint)
                        .lineLimit(1)
                }
            }
            Text(signal.title)
                .font(pal.serif(18))
                .lineLimit(3)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            Text(signal.prediction)
                .font(pal.sans(14.5))
                .lineSpacing(5)
                .foregroundStyle(pal.text3)
                .lineLimit(3)
                .multilineTextAlignment(.leading)
            HStack(spacing: 5) {
                Text(basisLine)
                    .font(pal.mono(12.5))
                Spacer()
                LastToldLabel(at: signal.createdAt)
                Image(systemName: "chevron.right").font(.caption2)
            }
            .foregroundStyle(pal.faint)
        }
    }
}

// MARK: - Signal deep-dive

/// The forecast, as a connected thread (mockup 3b, right panel).
///
/// Inverted, like "Read for you" and the lead card on What's Next, and for the
/// same reason: this page is our reasoning, not something that was reported.
/// The colours are fixed here rather than taken from `Palette` because the
/// design fixes them — the palette describes the paper, and this is not on it.
///
/// The mockup's "How often we get it right · 19 right · 7 wrong · 5 open" is
/// not drawn. Grading a forecast means revisiting it after its date passes and
/// recording what happened, which has never been done — see `NextView`.
struct SignalDetailView: View {
    @Environment(\.palette) private var pal
    @Environment(\.dismiss) private var dismiss

    let signal: Signal
    @EnvironmentObject var api: APIClient
    @State private var openStoryID: String?
    @State private var threadExpanded = false

    private let ground = Color.hex(0x17150F)
    private let paper = Color.hex(0xF7F4EE)
    private let gold = Color.hex(0xC8B98F)
    private var dim: Color { paper.opacity(0.7) }
    private var faint: Color { paper.opacity(0.62) }

    var body: some View {
        ZStack {
            ground.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    kicker
                    Text(signal.title)
                        .font(.system(size: 27, weight: .light, design: .serif))
                        .lineSpacing(3)
                        .kerning(-0.4)
                        .foregroundStyle(paper)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 14)
                    Text(signal.prediction)
                        .font(.system(size: 15))
                        .lineSpacing(7)
                        .foregroundStyle(dim)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 24)

                    if signal.isLocked { locked } else { thread }

                    if signal.falsifier?.isEmpty == false || !signal.watch.isEmpty { toWatch }
                    if let affected = signal.affected, !affected.isEmpty { lands(affected) }
                    trackRecord
                    Text("This forecast was written by a machine from stories we had already checked. The likelihood is how strongly those stories point the same way — not a measurement of how often we are right.")
                        .font(.system(size: 13))
                        .lineSpacing(4)
                        .foregroundStyle(paper.opacity(0.5))
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 20)
                }
                .padding(.horizontal, 22)
                .padding(.top, 12)
                .padding(.bottom, 40)
            }
            .scrollIndicators(.hidden)
        }
        .onStoryLink { openStoryID = $0 }
        .navigationDestination(item: $openStoryID) { StoryDetailView(storyID: $0) }
        // The floating tab bar is light and sits at the bottom of a page that
        // is deliberately dark; it also covers the track-record block.
        .toolbar(.hidden, for: .tabBar)
        .toolbarBackground(ground, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                ShareLink(item: api.shareURL("signal/\(signal.id)"),
                          subject: Text(signal.title),
                          message: Text("\(signal.title): \(signal.prediction) — via Descry")) {
                    Image(systemName: "square.and.arrow.up").foregroundStyle(gold)
                }
                .accessibilityLabel("Share this forecast")
            }
        }
    }

    private var kicker: some View {
        HStack(spacing: 9) {
            Text("Forecast · \(signal.horizon.flatMap { $0.isEmpty ? nil : $0 } ?? "no date given")")
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(gold)
            Text(Odds.word(signal.confidence))
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(gold)
                .padding(.horizontal, 9).padding(.vertical, 3)
                .background(Capsule().fill(gold.opacity(0.18)))
            Spacer(minLength: 0)
        }
        .padding(.bottom, 14)
    }

    // MARK: - The thread

    /// The stories, joined by a line. Each one carries its own agreement
    /// sentence and date, so the reader can see the forecast is standing on
    /// checked reporting — and how well checked each piece of it is.
    /// Newest first, like the mockup's chain. Capped until asked: forecasts in
    /// this catalogue cite as many as 39 stories, and 39 nodes is a wall rather
    /// than a chain — but none of them are hidden, the count is stated and the
    /// rest are one tap away.
    private static let threadCap = 5

    private var chain: [FeedItem] {
        signal.stories.sorted { ($0.createdAt ?? 0) > ($1.createdAt ?? 0) }
    }
    private var shown: [FeedItem] {
        threadExpanded ? chain : Array(chain.prefix(Self.threadCap))
    }

    private var thread: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("The stories behind this")
                .font(.system(size: 18, weight: .medium, design: .serif))
                .foregroundStyle(paper)
                .padding(.bottom, 13)
            ForEach(shown) { s in
                NavigationLink(value: s) { node(s) }
                    .buttonStyle(.plain)
            }
            if chain.count > Self.threadCap, !threadExpanded {
                Button { withAnimation(BL.spring) { threadExpanded = true } } label: {
                    HStack(alignment: .top, spacing: 12) {
                        VStack(spacing: 0) {
                            Circle().fill(gold.opacity(0.4)).frame(width: 5, height: 5)
                                .padding(.top, 6)
                            Rectangle().fill(gold.opacity(0.4)).frame(width: 1)
                                .frame(maxHeight: .infinity)
                        }
                        .frame(width: 18)
                        Text("\(chain.count - Self.threadCap) more \(chain.count - Self.threadCap == 1 ? "story" : "stories")")
                            .font(.system(size: 14))
                            .foregroundStyle(gold)
                        Spacer(minLength: 0)
                    }
                    .padding(.bottom, 13)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            // The dashed node is the forecast itself — the thread's open end,
            // and the only node that has not happened.
            HStack(alignment: .top, spacing: 12) {
                VStack(spacing: 0) {
                    Circle().strokeBorder(gold, style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                        .frame(width: 8, height: 8)
                        .padding(.top, 6)
                }
                .frame(width: 18)
                Text("→ Where it goes next")
                    .font(.system(size: 15))
                    .foregroundStyle(gold)
                Spacer(minLength: 0)
            }
            if !signal.chain.isEmpty {
                Text(signal.chain)
                    .font(.system(size: 14.5))
                    .lineSpacing(6)
                    .foregroundStyle(dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 30)
                    .padding(.top, 8)
            }
        }
        .padding(.bottom, 22)
    }

    private func node(_ s: FeedItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                Circle().fill(gold).frame(width: 7, height: 7).padding(.top, 6)
                Rectangle().fill(gold.opacity(0.4)).frame(width: 1)
                    .frame(maxHeight: .infinity)
            }
            .frame(width: 18)
            VStack(alignment: .leading, spacing: 3) {
                Text(s.headline)
                    .font(.system(size: 15))
                    .lineSpacing(3)
                    .foregroundStyle(paper)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Text(nodeMeta(s))
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(faint)
            }
            Spacer(minLength: 0)
        }
        .padding(.bottom, 13)
        .contentShape(Rectangle())
    }

    /// "most sources agree · 82 · 3 Aug" — the same scale the rest of the app
    /// uses, in lower case because it is running as a meta line here.
    private func nodeMeta(_ s: FeedItem) -> String {
        var parts = [AgreementBand.sentence(s.credibility).lowercased()]
        if let at = s.createdAt, at > 0 {
            parts.append(Date(timeIntervalSince1970: at)
                .formatted(.dateTime.day().month(.abbreviated)))
        }
        return parts.joined(separator: " · ")
    }

    // MARK: - Blocks

    /// The design's "What would stop it". Printed as a disproof when the
    /// forecast carries one, and as the confirming indicator it actually has
    /// otherwise — see the note in NextView.
    private var toWatch: some View {
        let disproof = signal.falsifier?.isEmpty == false
        return VStack(alignment: .leading, spacing: 9) {
            Text(disproof ? "What would stop it" : "What to watch")
                .font(.system(size: 16, weight: .medium, design: .serif))
                .foregroundStyle(paper)
            Text(disproof ? signal.falsifier! : signal.watch)
                .font(.system(size: 14))
                .lineSpacing(5)
                .foregroundStyle(paper.opacity(0.72))
                .fixedSize(horizontal: false, vertical: true)
            if disproof, !signal.watch.isEmpty {
                Text("On track if: \(signal.watch)")
                    .font(.system(size: 13))
                    .lineSpacing(4)
                    .foregroundStyle(paper.opacity(0.55))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(paper.opacity(0.14), lineWidth: 1))
        .padding(.bottom, 14)
    }

    private func lands(_ affected: [String]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Who this lands on")
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(paper.opacity(0.55))
            BLFlow(spacing: 7, lineSpacing: 7) {
                ForEach(affected, id: \.self) { who in
                    Text(who)
                        .font(.system(size: 13.5))
                        .foregroundStyle(paper.opacity(0.85))
                        .padding(.horizontal, 13).padding(.vertical, 7)
                        .overlay(Capsule().stroke(paper.opacity(0.28), lineWidth: 1))
                }
            }
        }
        .padding(.bottom, 18)
    }

    /// Where the mockup prints the hit rate. We have no graded forecasts, so
    /// this says that instead of drawing a bar with nothing behind it.
    private var trackRecord: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("How often we get it right")
                .font(.system(size: 17, weight: .medium, design: .serif))
                .foregroundStyle(ground)
            Text("We don't know yet, and we won't guess. No forecast has been revisited after its date to record what actually happened. When they have been, the count — misses included — will be here.")
                .font(.system(size: 13.5))
                .lineSpacing(5)
                .foregroundStyle(Color.hex(0x5C574F))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(paper))
    }

    /// The server withheld the reasoning, the watch-list and the stories. Say
    /// plainly what is behind the wall — a locked feature only earns a sign-up
    /// if the reader can tell what they would be getting.
    private var locked: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("The working is behind sign-in")
                .font(.system(size: 16, weight: .medium, design: .serif))
                .foregroundStyle(paper)
            Text("The \(signal.storyCount ?? 0) \((signal.storyCount ?? 0) == 1 ? "story" : "stories") this stands on, how they were connected, and what would change our mind. It is the part worth having, so it is the part we ask you to sign in for.")
                .font(.system(size: 14))
                .lineSpacing(5)
                .foregroundStyle(paper.opacity(0.72))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(gold.opacity(0.3), lineWidth: 1))
        .padding(.bottom, 18)
    }
}

struct FlowAffectedChips: View {
    let items: [String]
    var body: some View {
        // BLFlow, not LazyVGrid(.adaptive): the grid handed every chip a fixed
        // column width, so any label wider than the column spilled outside its
        // own capsule. Flow lets each chip size to its text and wraps to the
        // next line when it runs out of width.
        BLFlow(spacing: 8, lineSpacing: 8) {
            ForEach(items.prefix(8), id: \.self) { Chip(text: $0) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
