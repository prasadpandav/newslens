import SwiftUI

/// Trend Radar — macro forces and 72-hour early signals.
struct TrendsView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @State private var trends: [Trend] = []
    @State private var signals: [Signal] = []
    @State private var tab = "signals"
    @State private var loading = true
    @Environment(\.scenePhase) private var scenePhase

    private var filteredTrends: [Trend] { trends.filter { $0.kind == "macro" } }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Trends")
                                .font(.system(.largeTitle, design: .serif, weight: .semibold))
                            Text("what's building up — and what may happen next")
                                .font(.footnote).foregroundStyle(pal.text2)
                        }
                        .padding(.top, 8)

                        Picker("Kind", selection: $tab.animation(BL.spring)) {
                            Text("What may happen").tag("signals")
                            Text("Big trends").tag("macro")
                        }
                        .pickerStyle(.segmented)

                        if loading {
                            ProgressView().tint(pal.accent)
                                .frame(maxWidth: .infinity).padding(.top, 60)
                        } else if tab == "signals" {
                            if signals.isEmpty {
                                ContentUnavailableView("No predictions yet",
                                    systemImage: "waveform.path.ecg",
                                    description: Text("Predictions appear once there are enough stories across different topics to connect."))
                            } else {
                                LazyVStack(spacing: 12) {
                                    ForEach(signals) { sig in
                                        NavigationLink(value: sig) { SignalCard(signal: sig) }
                                            .buttonStyle(.plain)
                                            .scrollTransition(.animated(BL.spring)) { view, phase in
                                                view.opacity(phase.isIdentity ? 1 : 0.4)
                                                    .scaleEffect(phase.isIdentity ? 1 : 0.97)
                                            }
                                    }
                                }
                            }
                        } else if filteredTrends.isEmpty {
                            ContentUnavailableView("No big trends yet",
                                systemImage: "chart.line.uptrend.xyaxis",
                                description: Text("Run the backend pipeline, then pull to refresh."))
                        } else {
                            LazyVStack(spacing: 12) {
                                ForEach(filteredTrends) { t in
                                    NavigationLink(value: t) { TrendCard(trend: t) }
                                        .buttonStyle(.plain)
                                        .scrollTransition(.animated(BL.spring)) { view, phase in
                                            view.opacity(phase.isIdentity ? 1 : 0.4)
                                                .scaleEffect(phase.isIdentity ? 1 : 0.97)
                                        }
                                }
                            }
                        }
                    }
                    .padding(.horizontal, 18)
                    .padding(.bottom, 40)
                }
                .scrollIndicators(.hidden)
            }
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: Trend.self) { TrendDetailView(trend: $0) }
            .navigationDestination(for: Signal.self) { SignalDetailView(signal: $0) }
            .navigationDestination(for: FeedItem.self) { StoryDetailView(storyID: $0.id) }
            .task { await load() }
            .refreshable { await load() }
            // Like BriefView, this view lives inside a persistent TabView, so
            // .task fires once per app launch and never again — without this,
            // trends/signals ranked on day one stay pinned at the top no
            // matter how long the app stays backgrounded and resumed.
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await load() } }
            }
        }
    }

    private func load() async {
        async let t = api.fetchTrends()
        async let s = api.fetchSignals()
        trends = (try? await t) ?? []
        signals = (try? await s) ?? []
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

struct TrendCard: View {
    @Environment(\.palette) private var pal

    let trend: Trend

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Text(BriefView.cleanName(trend.name))
                    .font(.headline)
                    .multilineTextAlignment(.leading)
                Spacer()
                Sparkline(seed: trend.name,
                          color: trend.kind == "micro" ? pal.prediction : pal.accent,
                          width: 84, height: 26)
            }
            Text(trend.narrative)
                .font(.subheadline).foregroundStyle(pal.text2)
            HStack(spacing: 8) {
                ForEach((trend.sectors ?? []).prefix(3), id: \.self) { s in
                    Chip(text: s)
                }
                if trend.kind == "micro" {
                    Chip(text: "picking up speed", color: pal.prediction, filled: true)
                }
                Spacer()
                Text("\(trend.articleCount ?? 0) stories")
                    .font(.caption2.monospaced()).foregroundStyle(pal.text2)
                LastToldLabel(at: trend.createdAt)
            }
        }
        .padding(18)
        .blCard()
    }
}
