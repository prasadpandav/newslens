import SwiftUI

/// Stories the user has read (opened) or explicitly dismissed — kept out of the
/// main feed so it doesn't go stale, but never actually gone.
struct ReadView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @State private var items: [FeedItem] = []
    @State private var loading = true

    /// "8 read · 4 topics" — the design's Read page is an information diet, not
    /// a score, so it counts what you have heard rather than how often.
    private var countLine: String {
        guard !items.isEmpty else { return loading ? "reading…" : "nothing read yet" }
        let topics = Set(items.map { $0.topic.lowercased() }).count
        return "\(items.count) read · \(topics) \(topics == 1 ? "topic" : "topics")"
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                PageHeader(title: "Read", subtitle: countLine)
                if loading {
                    Spacer()
                    ProgressView().tint(pal.accent)
                    Spacer()
                } else if items.isEmpty {
                    Spacer()
                    ContentUnavailableView {
                        Label("Nothing read yet", systemImage: "checkmark.circle")
                    } description: {
                        Text("Stories you open or mark as read will show up here, out of your main feed.")
                    }
                    Spacer()
                } else {
                    ScrollView {
                        LazyVStack(spacing: 14) {
                            ForEach(items) { item in
                                NavigationLink(value: item) { StoryCard(item: item) }
                                    .buttonStyle(.plain)
                                    .contextMenu {
                                        Button {
                                            Task {
                                                await api.unmarkRead(storyID: item.id)
                                                items.removeAll { $0.id == item.id }
                                            }
                                        } label: {
                                            Label("Move back to feed", systemImage: "arrow.uturn.backward")
                                        }
                                    }
                            }
                        }
                        .padding(.horizontal, 18)
                        .padding(.vertical, 12)
                    }
                    .scrollIndicators(.hidden)
                }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(for: FeedItem.self) { StoryDetailView(storyID: $0.id) }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        items = (try? await api.fetchReadStories()) ?? []
        // Drop stories un-marked from elsewhere since last load.
        items = items.filter { api.readStoryIDs.isEmpty || api.readStoryIDs.contains($0.id) }
        loading = false
    }
}
