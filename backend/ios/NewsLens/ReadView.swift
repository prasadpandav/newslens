import SwiftUI

/// Stories the user has read (opened) or explicitly dismissed — kept out of the
/// main feed so it doesn't go stale, but never actually gone.
struct ReadView: View {
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
                        Label("Nothing read yet", systemImage: "checkmark.circle")
                    } description: {
                        Text("Stories you open or mark as read will show up here, out of your main feed.")
                    }
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
            .navigationTitle("Read")
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
