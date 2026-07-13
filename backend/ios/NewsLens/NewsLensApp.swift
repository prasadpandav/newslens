import SwiftUI

@main
struct NewsLensApp: App {
    @StateObject private var api = APIClient.shared
    @AppStorage("onboarded") private var onboarded = false

    var body: some Scene {
        WindowGroup {
            Group {
                if onboarded {
                    RootTabs()
                } else {
                    OnboardingView { onboarded = true }
                }
            }
            .environmentObject(api)
            .preferredColorScheme(.dark)   // dark-first design language
        }
    }
}

struct RootTabs: View {
    var body: some View {
        // On iOS 26 the system tab bar renders in Liquid Glass automatically.
        TabView {
            BriefView()
                .tabItem { Label("Brief", systemImage: "sun.max") }
            TrendsView()
                .tabItem { Label("Radar", systemImage: "chart.line.uptrend.xyaxis") }
        }
        .tint(BL.accent)
    }
}
