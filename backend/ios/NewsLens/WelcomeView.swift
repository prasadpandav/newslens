import SwiftUI
#if canImport(GoogleSignIn)
import GoogleSignIn
#endif
#if canImport(GoogleSignInSwift)
import GoogleSignInSwift
#endif

/// First-launch screen: sign in with Google, or continue as guest.
/// Onboarding is NOT forced here — personalization is opt-in from the Brief.
/// The Google "G" drawn natively — four brand-color arcs plus the crossbar.
/// Crisp at any size, adapts to our card surfaces without a white box.
struct GoogleGMark: View {
    var size: CGFloat = 18
    private let blue = Color(red: 0.26, green: 0.52, blue: 0.96)
    private let green = Color(red: 0.20, green: 0.66, blue: 0.33)
    private let yellow = Color(red: 0.98, green: 0.74, blue: 0.02)
    private let red = Color(red: 0.92, green: 0.26, blue: 0.21)

    var body: some View {
        let stroke = size * 0.21
        ZStack {
            segment(0.000, 0.125, blue, stroke)    // right (below crossbar)
            segment(0.125, 0.375, green, stroke)   // bottom
            segment(0.375, 0.625, yellow, stroke)  // left
            segment(0.625, 0.875, red, stroke)     // top (gap at top-right)
            Rectangle()                            // the G crossbar
                .fill(blue)
                .frame(width: size * 0.48, height: stroke)
                .offset(x: size * 0.26)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private func segment(_ from: CGFloat, _ to: CGFloat, _ color: Color,
                         _ width: CGFloat) -> some View {
        Circle()
            .trim(from: from, to: to)
            .stroke(color, style: StrokeStyle(lineWidth: width))
            .padding(width / 2)
    }
}

struct WelcomeView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    var onDone: () -> Void

    @State private var signingIn = false
    @State private var error: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drift = false

    var body: some View {
        ZStack {
            InkBackground()
            orbs
            VStack(spacing: 0) {
                Spacer()
                Image(systemName: "circle.hexagongrid.circle")
                    .font(.system(size: 56))
                    .foregroundStyle(pal.aiGradient)
                Text("Descry")
                    .font(.system(.largeTitle, design: .serif, weight: .semibold))
                    .padding(.top, 14)
                Text("News that tell stories, stories that matter.")
                    .font(.system(.callout, design: .serif))
                    .italic()
                    .foregroundStyle(pal.accent)
                    .multilineTextAlignment(.center)
                    .padding(.top, 6)
                    .padding(.horizontal, 24)
                Text("Understand the news.\nNot just read it — go deeper.")
                    .font(.title3)
                    .foregroundStyle(pal.text2)
                    .multilineTextAlignment(.center)
                    .padding(.top, 8)
                Spacer()
                VStack(spacing: 12) {
                    googleButton
                        .disabled(signingIn)

                    Button {
                        onDone()   // guest mode; an anonymous account is created on demand
                    } label: {
                        Text("Skip for now")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(pal.text2)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 12)
                    }

                    if let error {
                        Text(error).font(.caption).foregroundStyle(pal.breaking)
                            .multilineTextAlignment(.center)
                    }
                    Text("Always free — no payment, ever. Signing in keeps your saved articles and personalization across devices and reinstalls.")
                        .font(.caption2).foregroundStyle(pal.text2.opacity(0.8))
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal, 28)
                .padding(.bottom, 40)
            }
        }
    }

    private var orbs: some View {
        ZStack {
            Circle().fill(pal.accent.opacity(0.16)).frame(width: 280)
                .blur(radius: 76)
                .offset(x: drift ? 100 : -70, y: drift ? -200 : -140)
            Circle().fill(pal.ai.opacity(0.13)).frame(width: 320)
                .blur(radius: 84)
                .offset(x: drift ? -90 : 70, y: drift ? 280 : 220)
        }
        .accessibilityHidden(true)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 10).repeatForever(autoreverses: true)) {
                drift = true
            }
        }
    }

    /// Sleek, on-theme sign-in button that keeps the recognizable Google "G".
    private var googleButton: some View {
        Button(action: signIn) {
            HStack(spacing: 12) {
                GoogleGMark(size: 18)
                Text(signingIn ? "Signing in…" : "Continue with Google")
                    .font(.subheadline.weight(.semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 15)
            .background(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                .fill(pal.surface)
                .overlay(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                    .stroke(pal.hairline2, lineWidth: 1)))
        }
        .buttonStyle(.plain)
    }

    private func signIn() {
        #if canImport(GoogleSignIn)
        // Present from the topmost VC of the foreground-active scene — the
        // most reliable anchor for the Safari sign-in sheet.
        guard let scene = UIApplication.shared.connectedScenes
                .first(where: { $0.activationState == .foregroundActive }) as? UIWindowScene,
              let root = scene.keyWindow?.rootViewController else { return }
        var presenter = root
        while let presented = presenter.presentedViewController { presenter = presented }
        signingIn = true
        error = nil
        GIDSignIn.sharedInstance.signIn(withPresenting: presenter) { result, err in
            Task { @MainActor in
                defer { signingIn = false }
                if err != nil {
                    // user cancelled or SDK error — stay on this screen
                    return
                }
                guard let idToken = result?.user.idToken?.tokenString else {
                    error = "Google didn't return a valid token. Try again."
                    return
                }
                do {
                    let hasContext = try await api.signInWithGoogle(idToken: idToken)
                    if hasContext {
                        // returning user: restore their saved context locally too
                        UserDefaults.standard.set(true, forKey: "onboarded")
                    }
                    onDone()
                } catch {
                    self.error = "Couldn't reach the Descry server. Check your connection and try again."
                }
            }
        }
        #else
        error = "Google Sign-In SDK is not added yet. In Xcode: File → Add Package Dependencies → https://github.com/google/GoogleSignIn-iOS — or use Skip for now."
        #endif
    }
}
