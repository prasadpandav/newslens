import SwiftUI

/// Profile — account, personalization details ("your lens"), learning stats.
struct ProfileView: View {
    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @AppStorage("onboarded") private var onboarded = false
    @AppStorage("welcomed") private var welcomed = false
    @State private var showEdit = false
    @State private var confirmSignOut = false
    @State private var showSignIn = false
    @State private var showLiveConfig = false
    @State private var livePrefs = LivePrefs.default

    private var ctx: UserContext? {
        guard let data = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: data)
    }

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        accountCard
                        lensCard
                        liveCard
                        statsCard
                        if api.isGoogleUser {
                            Button(role: .destructive) { confirmSignOut = true } label: {
                                Text("Sign out")
                                    .font(.subheadline.weight(.semibold))
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 13)
                            }
                            .blCard(radius: 14)
                        } else {
                            Button { showSignIn = true } label: {
                                Label("Sign in with Google", systemImage: "person.crop.circle.badge.checkmark")
                                    .font(.subheadline.weight(.semibold))
                                    .frame(maxWidth: .infinity)
                                    .padding(.vertical, 13)
                                    .foregroundStyle(.white)
                                    .background(RoundedRectangle(cornerRadius: 14, style: .continuous)
                                        .fill(BL.aiGradient))
                            }
                            Text("Free, always — sign in to keep your saved articles and personalization across devices.")
                                .font(.caption2).foregroundStyle(BL.text2)
                        }
                        Text("Your personalization details are stored on your Descry account and used only to explain what stories mean for you.")
                            .font(.caption2).foregroundStyle(BL.text2.opacity(0.8))
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 12)
                }
                .scrollIndicators(.hidden)
            }
            .navigationTitle("Profile")
            .task {
                await api.refreshProfile()
                if let p = ctx?.livePrefs { livePrefs = p }
            }
            .sheet(isPresented: $showLiveConfig) {
                LiveConfigView(prefs: livePrefs) { livePrefs = $0 }
                    .environmentObject(api)
            }
            .sheet(isPresented: $showSignIn) {
                WelcomeView { showSignIn = false }
                    .environmentObject(api)
            }
            .sheet(isPresented: $showEdit) {
                OnboardingView(initial: ctx) {
                    onboarded = true
                    showEdit = false
                }
                .environmentObject(api)
            }
            .confirmationDialog("Sign out of Descry?", isPresented: $confirmSignOut) {
                Button("Sign out", role: .destructive) {
                    api.signOut()
                    onboarded = false
                    welcomed = false
                }
            } message: {
                Text("Your saved articles and personalization stay on your account — sign back in anytime to restore them.")
            }
        }
    }

    private var accountCard: some View {
        HStack(spacing: 14) {
            ZStack {
                if let urlStr = api.userPhotoURL, !urlStr.isEmpty,
                   let url = URL(string: urlStr) {
                    AsyncImage(url: url) { image in
                        image.resizable().scaledToFill()
                    } placeholder: {
                        Circle().fill(BL.aiGradient)
                    }
                    .frame(width: 52, height: 52)
                    .clipShape(Circle())
                } else {
                    Circle().fill(BL.aiGradient).frame(width: 52, height: 52)
                    Text(String((api.displayName ?? "G").prefix(1)).uppercased())
                        .font(.title3.weight(.bold)).foregroundStyle(.white)
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(api.displayName?.isEmpty == false ? api.displayName! : "Guest reader")
                    .font(.headline)
                Text(api.isGoogleUser ? (api.userEmail ?? "")
                     : "Guest · sign in to sync across devices")
                    .font(.caption).foregroundStyle(BL.text2)
            }
            Spacer()
        }
        .padding(16)
        .blCard()
    }

    private var lensCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("YOUR LENS", systemImage: "scope")
                    .font(.caption2.weight(.bold)).kerning(1)
                    .foregroundStyle(BL.accent)
                Spacer()
                Button(ctx == nil ? "Set up" : "Edit") { showEdit = true }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(BL.accent)
            }
            if let ctx {
                row("Interests", ctx.interests.joined(separator: ", "))
                row("Profession", ctx.profession)
                row("Business", ctx.lineOfBusiness)
                row("Role", ctx.roleSeniority.capitalized)
                row("Location", [ctx.location.city, ctx.location.region, ctx.location.country]
                    .filter { !$0.isEmpty }.joined(separator: ", "))
                row("Native language", ctx.nativeLanguage)
                row("Reads news in", ctx.preferredLanguage)
                if !ctx.micro.isEmpty {
                    row("Personal details", ctx.micro.map { "\($0.key): \($0.value)" }
                        .sorted().joined(separator: " · "))
                }
            } else {
                Text("Not personalized yet. Tell Descry your world once, and every story explains what it means for you.")
                    .font(.footnote).foregroundStyle(BL.text2)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .blCard()
    }

    private var liveCard: some View {
        Button { showLiveConfig = true } label: {
            HStack(spacing: 12) {
                Image(systemName: "bolt.horizontal.circle.fill")
                    .font(.title3).foregroundStyle(BL.breaking)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Home live section")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                    Text(livePrefs.enabled
                         ? liveSummary : "Off — tap to enable breaking, scores & more")
                        .font(.caption).foregroundStyle(BL.text2)
                        .multilineTextAlignment(.leading)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(BL.text2)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .blCard()
        }
        .buttonStyle(.plain)
    }

    private var liveSummary: String {
        let names = livePrefs.categories
            .compactMap { LiveCategory(rawValue: $0)?.label }
        return names.isEmpty ? "No categories selected" : names.joined(separator: " · ")
    }

    private var statsCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("YOUR INTELLIGENCE", systemImage: "brain.head.profile")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.prediction)
            HStack {
                stat("flame.fill", "\(eng.streak)", "day streak", BL.warning)
                Divider().frame(height: 34).overlay(BL.hairline)
                stat("checkmark.seal.fill", "\(eng.understood)", "completed", BL.trust)
                Divider().frame(height: 34).overlay(BL.hairline)
                stat("safari.fill", "\(eng.topics.count)", "topics", BL.accent)
                Divider().frame(height: 34).overlay(BL.hairline)
                stat("bookmark.fill", "\(api.savedStoryIDs.count)", "saved", BL.prediction)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .blCard()
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.caption).foregroundStyle(BL.text2)
                .frame(width: 110, alignment: .leading)
            Text(value.isEmpty ? "—" : value).font(.footnote)
            Spacer(minLength: 0)
        }
    }

    private func stat(_ icon: String, _ value: String, _ label: String, _ color: Color) -> some View {
        VStack(spacing: 3) {
            Image(systemName: icon).font(.footnote).foregroundStyle(color)
            Text(value).font(.headline.monospaced())
            Text(label).font(.caption2).foregroundStyle(BL.text2)
        }
        .frame(maxWidth: .infinity)
    }
}
