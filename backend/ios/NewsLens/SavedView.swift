import SwiftUI

/// Saved articles — bookmarks synced with the backend.
struct SavedView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @State private var items: [FeedItem] = []
    @State private var loading = true

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                if loading {
                    ProgressView().tint(pal.accent)
                } else if items.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing saved yet", systemImage: "bookmark")
                    } description: {
                        Text("Tap the bookmark on any story to keep it here for later.")
                    }
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
            .navigationTitle("Saved")
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
