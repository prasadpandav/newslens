import SwiftUI

/// Daily Brief — the redesigned home. Greeting, intelligence summary, glass topic
/// filter, story cards with scroll-driven motion and zoom hero transitions.
struct BriefView: View {
    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @State private var items: [FeedItem] = []
    @State private var trends: [Trend] = []
    @State private var topic = "all"
    @State private var loading = true
    @State private var error: String?
    @Namespace private var zoomNS

    private var topics: [String] { ["all"] + Array(Set(items.map(\.topic))).sorted() }
    private var filtered: [FeedItem] { topic == "all" ? items : items.filter { $0.topic == topic } }
    private var greeting: String {
        let h = Calendar.current.component(.hour, from: .now)
        return h < 12 ? "Good morning." : h < 17 ? "Good afternoon." : "Good evening."
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                if loading {
                    ProgressView("Building your lens…").tint(BL.accent)
                } else if let error {
                    ContentUnavailableView("Can't reach the backend",
                                           systemImage: "wifi.exclamationmark",
                                           description: Text(error))
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
            .task { await load() }
            .refreshable { await load() }
        }
        .preferredColorScheme(.dark)
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                if !microTrends.isEmpty { signalsStrip }
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
                    }
                }
                statsCard
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
    }

    private var microTrends: [Trend] { trends.filter { $0.kind == "micro" }.prefix(4).map { $0 } }
    private var topSignal: String {
        trends.first(where: { $0.kind == "macro" }).map { Self.cleanName($0.name) } ?? "—"
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(greeting + " Here's what matters.")
                    .font(.system(.largeTitle, design: .serif, weight: .semibold))
                Spacer()
            }
            HStack(spacing: 8) {
                Chip(text: Date.now.formatted(.dateTime.weekday(.wide).month().day()))
                Chip(text: "🔥 \(eng.streak)-day streak", color: BL.warning, filled: true)
            }
            Text("**\(filtered.count) stories** · ~\(max(2, filtered.count / 2)) min to understand · top signal: **\(topSignal)**")
                .font(.footnote)
                .foregroundStyle(BL.text2)
        }
        .padding(.top, 8)
    }

    private var signalsStrip: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("EARLY SIGNALS · 72H", systemImage: "waveform.path.ecg")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.prediction)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(microTrends) { t in
                        NavigationLink(value: t) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(Self.cleanName(t.name))
                                    .font(.footnote.weight(.semibold))
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                                Text(t.narrative)
                                    .font(.caption2).foregroundStyle(BL.text2)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                                HStack(spacing: 4) {
                                    Text("Explore").font(.caption2.weight(.semibold))
                                    Image(systemName: "chevron.right").font(.caption2)
                                }
                                .foregroundStyle(BL.prediction)
                            }
                            .padding(12)
                            .frame(width: 210, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(BL.prediction.opacity(0.08))
                                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .stroke(BL.prediction.opacity(0.25), lineWidth: 1)))
                        }
                        .buttonStyle(.plain)
                    }
                }
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
                        Chip(text: t.capitalized,
                             color: t == topic ? BL.accent : BL.text2,
                             filled: t == topic)
                    }
                }
            }
            .padding(.vertical, 4)
        }
    }

    private var statsCard: some View {
        HStack {
            stat("flame.fill", "\(eng.streak)", "day streak", BL.warning)
            Divider().frame(height: 34).overlay(BL.hairline)
            stat("checkmark.seal.fill", "\(eng.understood)", "understood", BL.trust)
            Divider().frame(height: 34).overlay(BL.hairline)
            stat("safari.fill", "\(eng.topics.count)", "topics", BL.accent)
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
            Text(label).font(.caption2).foregroundStyle(BL.text2)
        }
        .frame(maxWidth: .infinity)
    }

    private func load() async {
        do {
            async let f = api.fetchFeed()
            async let t = api.fetchTrends()
            items = try await f
            trends = (try? await t) ?? []
            error = nil
        } catch {
            self.error = "Start the backend (uvicorn app.main:app) and check APIClient.baseURL."
        }
        loading = false
    }
}

// MARK: - Story card

struct StoryCard: View {
    let item: FeedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Chip(text: item.topic.capitalized)
                if item.credibility >= 85 { Chip(text: "Highly corroborated", color: BL.trust, filled: true) }
                Spacer()
                ImpactBadge(score: item.impactScore ?? 0)
            }
            Text(item.headline)
                .font(.system(.title3, design: .serif, weight: .semibold))
                .lineSpacing(1)
                .multilineTextAlignment(.leading)
            Text(item.narrative)
                .font(.subheadline)
                .foregroundStyle(BL.text2)
                .lineLimit(3)
            if let impact = item.impactText, !impact.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.exclamationmark")
                        .font(.caption).foregroundStyle(BL.accent)
                    Text(impact).font(.caption).foregroundStyle(BL.text2).lineLimit(2)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(BL.accent.opacity(0.08)))
            }
            TrustMeter(score: item.credibility)
        }
        .padding(18)
        .blCard()
    }
}
