import SwiftUI

/// Trend Radar — macro forces and 72-hour early signals.
struct TrendsView: View {
    @EnvironmentObject var api: APIClient
    @State private var trends: [Trend] = []
    @State private var tab = "macro"
    @State private var loading = true

    private var filtered: [Trend] { trends.filter { $0.kind == tab } }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Trend Radar")
                                .font(.system(.largeTitle, design: .serif, weight: .semibold))
                            Text("macro forces & 72-hour early signals")
                                .font(.footnote).foregroundStyle(BL.text2)
                        }
                        .padding(.top, 8)

                        Picker("Kind", selection: $tab.animation(BL.spring)) {
                            Text("Macro trends").tag("macro")
                            Text("Early signals").tag("micro")
                        }
                        .pickerStyle(.segmented)

                        if loading {
                            ProgressView().tint(BL.accent)
                                .frame(maxWidth: .infinity).padding(.top, 60)
                        } else if filtered.isEmpty {
                            ContentUnavailableView(
                                tab == "macro" ? "No macro trends yet" : "No early signals yet",
                                systemImage: "chart.line.uptrend.xyaxis",
                                description: Text("Run the backend pipeline, then pull to refresh."))
                        } else {
                            LazyVStack(spacing: 12) {
                                ForEach(Array(filtered.enumerated()), id: \.element.id) { _, t in
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
            .navigationDestination(for: FeedItem.self) { StoryDetailView(storyID: $0.id) }
            .task { await load() }
            .refreshable { await load() }
        }
        .preferredColorScheme(.dark)
    }

    private func load() async {
        trends = (try? await api.fetchTrends()) ?? []
        loading = false
    }
}

// MARK: - Trend deep-dive

struct TrendDetailView: View {
    let trend: Trend
    @EnvironmentObject var api: APIClient
    @State private var detail: TrendDetail?

    var body: some View {
        ZStack {
            InkBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack(spacing: 8) {
                        Chip(text: trend.kind == "micro" ? "Early signal · 72h" : "Macro trend",
                             color: trend.kind == "micro" ? BL.prediction : BL.accent,
                             filled: true)
                        Spacer()
                        Sparkline(seed: trend.name,
                                  color: trend.kind == "micro" ? BL.prediction : BL.accent,
                                  width: 90, height: 26)
                    }
                    Text(BriefView.cleanName(trend.name))
                        .font(.system(.largeTitle, design: .serif, weight: .semibold))
                    Text((detail?.narrative ?? trend.narrative))
                        .font(.subheadline).foregroundStyle(BL.text2)
                    if let sectors = detail?.sectors ?? trend.sectors, !sectors.isEmpty {
                        HStack(spacing: 8) {
                            ForEach(sectors.prefix(4), id: \.self) { Chip(text: $0) }
                        }
                    }
                    Divider().overlay(BL.hairline)
                    Text("STORIES DRIVING THIS SIGNAL")
                        .font(.caption2.weight(.bold)).kerning(1)
                        .foregroundStyle(BL.text2)
                    if let stories = detail?.stories {
                        if stories.isEmpty {
                            Text("No stories linked yet — the next pipeline run will connect them.")
                                .font(.footnote).foregroundStyle(BL.text2)
                        } else {
                            LazyVStack(spacing: 12) {
                                ForEach(stories) { s in
                                    NavigationLink(value: s) { StoryCard(item: s) }
                                        .buttonStyle(.plain)
                                }
                            }
                        }
                    } else {
                        ProgressView().tint(BL.accent)
                            .frame(maxWidth: .infinity).padding(.top, 20)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.top, 8)
                .padding(.bottom, 40)
            }
            .scrollIndicators(.hidden)
        }
        .navigationBarTitleDisplayMode(.inline)
        .task { detail = try? await api.fetchTrendDetail(id: trend.id) }
        .preferredColorScheme(.dark)
    }
}

struct TrendCard: View {
    let trend: Trend

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Text(BriefView.cleanName(trend.name))
                    .font(.headline)
                    .multilineTextAlignment(.leading)
                Spacer()
                Sparkline(seed: trend.name,
                          color: trend.kind == "micro" ? BL.prediction : BL.accent,
                          width: 84, height: 26)
            }
            Text(trend.narrative)
                .font(.subheadline).foregroundStyle(BL.text2)
            HStack(spacing: 8) {
                ForEach((trend.sectors ?? []).prefix(3), id: \.self) { s in
                    Chip(text: s)
                }
                if trend.kind == "micro" {
                    Chip(text: "accelerating", color: BL.prediction, filled: true)
                }
                Spacer()
                Text("\(trend.articleCount ?? 0) stories")
                    .font(.caption2.monospaced()).foregroundStyle(BL.text2)
            }
        }
        .padding(18)
        .blCard()
    }
}
