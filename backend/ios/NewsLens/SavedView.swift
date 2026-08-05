import SwiftUI

/// Saved articles — bookmarks synced with the backend.
struct SavedView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @State private var items: [FeedItem] = []
    @State private var loading = true

    /// "12 saved · 3 moved since you saved them" — the design's Saved page is
    /// about what CHANGED while you were away, so the header counts that rather
    /// than just the pile.
    private var countLine: String {
        guard !items.isEmpty else { return loading ? "reading…" : "nothing saved" }
        let moved = items.filter { $0.correction != nil }.count
        var line = "\(items.count) saved"
        if moved > 0 { line += " · \(moved) moved since" }
        return line
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                PageHeader(title: "Saved", subtitle: countLine)
                if loading {
                    Spacer()
                    ProgressView().tint(pal.accent)
                    Spacer()
                } else if items.isEmpty {
                    Spacer()
                    ContentUnavailableView {
                        Label("Nothing saved yet", systemImage: "bookmark")
                    } description: {
                        Text("Tap the bookmark on any story to keep it here for later.")
                    }
                    Spacer()
                } else {
                    ScrollView {
                        LazyVStack(spacing: 14) {
                            ForEach(items) { item in
                                NavigationLink(value: item) { StoryCard(item: item) }
                                    .buttonStyle(.plain)
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
        items = (try? await api.fetchBookmarks()) ?? []
        // Drop stories the user un-saved from a detail screen since last load.
        items = items.filter { api.savedStoryIDs.isEmpty || api.savedStoryIDs.contains($0.id) }
        loading = false
    }
}
