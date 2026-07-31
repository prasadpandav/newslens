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

    /// The Brief's horizontal peek. Deliberately two lines of headline and one
    /// meta row — this strip sits above the feed, so every point it spends is a
    /// point of actual news the reader has to scroll past it to reach.
    private var compactBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "waveform.path.ecg")
                    .font(.caption2.weight(.semibold))
                Text("\(Int(signal.confidence * 100))%")
                    .font(.caption2.weight(.semibold).monospaced())
                Spacer(minLength: 4)
                Text("\(signal.stories.count) stories")
                    .font(.caption2)
                    .foregroundStyle(pal.text2)
            }
            .foregroundStyle(pal.prediction)
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
                Image(systemName: "waveform.path.ecg")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(pal.prediction)
                    .frame(width: 26, height: 26)
                    .background(RoundedRectangle(cornerRadius: pal.r(8), style: .continuous)
                        .fill(pal.prediction.opacity(0.13)))
                Spacer()
                Text("\(Int(signal.confidence * 100))% confidence")
                    .font(.caption2.weight(.semibold).monospaced())
                    .foregroundStyle(pal.prediction)
            }
            Text(signal.title)
                .font(.headline)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
            Text(signal.prediction)
                .font(.subheadline)
                .foregroundStyle(pal.text2)
                .lineLimit(3)
                .multilineTextAlignment(.leading)
            HStack(spacing: 5) {
                Image(systemName: "doc.on.doc").font(.caption2)
                Text("based on \(signal.stories.count) stories")
                    .font(.caption2.weight(.semibold))
                Spacer()
                LastToldLabel(at: signal.createdAt)
                Image(systemName: "chevron.right").font(.caption2)
            }
            .foregroundStyle(pal.text2)
        }
    }
}

// MARK: - Signal deep-dive

struct SignalDetailView: View {
    @Environment(\.palette) private var pal

    let signal: Signal
    @EnvironmentObject var api: APIClient
    @State private var openStoryID: String?

    var body: some View {
        ZStack {
            InkBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    BLFlow(spacing: 8) {
                        Chip(text: "Prediction", color: pal.prediction, filled: true)
                        if let horizon = signal.horizon, !horizon.isEmpty {
                            Chip(text: horizon)
                        }
                        LastToldChip(at: signal.createdAt)
                        confidenceBadge
                    }
                    Text(signal.title)
                        .font(.system(.title, design: .serif, weight: .semibold))
                        .lineLimit(4)
                        .minimumScaleFactor(0.8)

                    section("WHAT MAY HAPPEN", icon: "scope", tint: pal.prediction) {
                        LinkedText(text: signal.prediction, refs: signal.storyRefs ?? [])
                            .font(.subheadline)
                    }
                    if signal.isLocked {
                        // The server withheld the reasoning, watch-list and
                        // stories. Show what's waiting rather than empty cards.
                        lockedFooter
                    } else {
                    section("HOW WE CONNECTED THE DOTS", icon: "point.3.connected.trianglepath.dotted",
                            tint: pal.ai) {
                        LinkedText(text: signal.chain, refs: signal.storyRefs ?? [])
                            .font(.subheadline).foregroundStyle(pal.text2)
                            .lineSpacing(3)
                    }
                    if !signal.watch.isEmpty {
                        section("WHAT TO WATCH", icon: "eye", tint: pal.warning) {
                            Text(signal.watch).font(.subheadline).foregroundStyle(pal.text2)
                        }
                    }
                    if let affected = signal.affected, !affected.isEmpty {
                        section("WHO'S AFFECTED", icon: "person.2", tint: pal.accent) {
                            FlowAffectedChips(items: affected)
                        }
                    }

                    Divider().overlay(pal.hairline)
                    Text("THE \(signal.stories.count) STORIES BEHIND THIS")
                        .font(.caption2.weight(.bold)).kerning(1)
                        .foregroundStyle(pal.text2)
                    LazyVStack(spacing: 12) {
                        ForEach(signal.stories) { s in
                            NavigationLink(value: s) { StoryCard(item: s) }
                                .buttonStyle(.plain)
                        }
                    }
                    }   // end !isLocked

                    Text("This prediction was made by AI by connecting stories — treat it as a lead to watch, not a fact. The confidence number shows how strongly the stories point the same way.")
                        .font(.caption2).foregroundStyle(pal.text2.opacity(0.7))
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
                ShareLink(item: api.shareURL("signal/\(signal.id)"),
                          subject: Text(signal.title),
                          message: Text("\(signal.title): \(signal.prediction) — via Descry")) {
                    Image(systemName: "square.and.arrow.up")
                        .foregroundStyle(pal.accent)
                }
                .accessibilityLabel("Share this prediction")
            }
        }
    }

    /// Shown instead of the reasoning/watch/stories when the caller isn't signed
    /// in. States plainly what is behind the wall — a locked feature only earns
    /// a sign-up if the reader can tell what they'd be getting.
    private var lockedFooter: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Signed-in feature", systemImage: "lock.fill")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(pal.prediction)
            Text("See how Descry connected \(signal.storyCount ?? 0) "
                 + "\((signal.storyCount ?? 0) == 1 ? "story" : "stories") to reach this "
                 + "prediction, what to watch next, and the stories behind it.")
                .font(.subheadline).foregroundStyle(pal.text2)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
            .fill(pal.prediction.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
            .stroke(pal.prediction.opacity(0.3), lineWidth: 1))
    }

    private var confidenceBadge: some View {
        HStack(spacing: 5) {
            Circle().fill(confColor).frame(width: 7, height: 7)
            Text("\(Int(signal.confidence * 100))% confidence")
                .font(.caption2.weight(.semibold).monospaced())
                .foregroundStyle(confColor)
        }
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(Capsule().fill(confColor.opacity(0.12)))
    }

    private var confColor: Color {
        signal.confidence >= 0.65 ? pal.trust : signal.confidence >= 0.45 ? pal.warning : pal.text2
    }

    private func section<Content: View>(_ title: String, icon: String, tint: Color,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon)
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(tint)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .blCard(radius: 14)
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
