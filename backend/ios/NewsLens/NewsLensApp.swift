import SwiftUI
#if canImport(GoogleSignIn)
import GoogleSignIn
#endif

@main
struct DescryApp: App {
    @StateObject private var api = APIClient.shared
    @StateObject private var theme = ThemeStore.shared
    @AppStorage("welcomed") private var welcomed = false

    var body: some Scene {
        WindowGroup {
            Group {
                if welcomed || api.userID != nil {
                    RootTabs()
                } else {
                    WelcomeView { welcomed = true }
                }
            }
            .environmentObject(api)
            .environmentObject(theme)
            /* The single place the palette enters the view tree. Because it is
               an Environment value and `theme` is observed here, changing the
               skin re-publishes it to every descendant that reads
               `\.palette` — all of them, in the same frame. That is what stops
               one screen keeping the old colours while another shows the new
               ones: nothing caches a colour, they all read the same value. */
            .environment(\.palette, theme.palette)
            /* A picked skin is a deliberate, fixed look — it should not be
               half-overridden by the device's dark mode, which is the same call
               the web makes by letting :root[data-skin] beat the dark-mode
               block. The default skin keeps following the system. */
            .preferredColorScheme(theme.skin == .default ? nil : .light)
            .onOpenURL { url in
                #if canImport(GoogleSignIn)
                GIDSignIn.sharedInstance.handle(url)
                #endif
            }
        }
    }
}

struct RootTabs: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient

    var body: some View {
        // On iOS 26 the system tab bar renders in Liquid Glass automatically.
        TabView {
            BriefView()
                .tabItem { Label("Brief", systemImage: "sun.max") }
            TrendsView()
                .tabItem { Label("Trends", systemImage: "chart.line.uptrend.xyaxis") }
            SavedView()
                .tabItem { Label("Saved", systemImage: "bookmark") }
            ReadView()
                .tabItem { Label("Read", systemImage: "checkmark.circle") }
            ProfileView()
                .tabItem { Label("Profile", systemImage: "person.crop.circle") }
        }
        .tint(pal.accent)
        .task {
            await api.loadBookmarks()
            await api.loadReadStories()
        }
    }
}
