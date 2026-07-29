import SwiftUI
#if canImport(GoogleSignIn)
import GoogleSignIn
#endif

@main
struct DescryApp: App {
    @StateObject private var api = APIClient.shared
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
            .onOpenURL { url in
                #if canImport(GoogleSignIn)
                GIDSignIn.sharedInstance.handle(url)
                #endif
            }
        }
    }
}

struct RootTabs: View {
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
            ProfileView()
                .tabItem { Label("Profile", systemImage: "person.crop.circle") }
        }
        .tint(BL.accent)
        .task { await api.loadBookmarks() }
    }
}
