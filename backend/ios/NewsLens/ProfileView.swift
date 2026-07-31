import SwiftUI

/// Profile — account, personalization details ("your lens"), learning stats.
struct ProfileView: View {
    @Environment(\.palette) private var pal

    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @AppStorage("onboarded") private var onboarded = false
    @AppStorage("welcomed") private var welcomed = false
    @State private var showEdit = false
    @State private var confirmSignOut = false
    @State private var showSignIn = false
    @State private var showLiveConfig = false
    @State private var showTheme = false
    @State private var livePrefs = LivePrefs.default
    @EnvironmentObject var theme: ThemeStore

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
                        appearanceCard
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
                                    .background(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                                        .fill(pal.aiGradient))
                            }
                            Text("Free, always — sign in to keep your saved articles and personalization across devices.")
                                .font(.caption2).foregroundStyle(pal.text2)
                        }
                        Text("Your personalization details are stored on your Descry account and used only to explain what stories mean for you.")
                            .font(.caption2).foregroundStyle(pal.text2.opacity(0.8))
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
            .sheet(isPresented: $showTheme) {
                ThemePickerView().environmentObject(api).environmentObject(theme).skinned()
            }
            .sheet(isPresented: $showLiveConfig) {
                LiveConfigView(prefs: livePrefs) { livePrefs = $0 }
                    .environmentObject(api).skinned()
            }
            .sheet(isPresented: $showSignIn) {
                WelcomeView { showSignIn = false }
                    .environmentObject(api).skinned()
            }
            .sheet(isPresented: $showEdit) {
                OnboardingView(initial: ctx) {
                    onboarded = true
                    showEdit = false
                }
                .environmentObject(api).skinned()
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
                        Circle().fill(pal.aiGradient)
                    }
                    .frame(width: 52, height: 52)
                    .clipShape(Circle())
                } else {
                    Circle().fill(pal.aiGradient).frame(width: 52, height: 52)
                    Text(String((api.displayName ?? "G").prefix(1)).uppercased())
                        .font(.title3.weight(.bold)).foregroundStyle(.white)
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(api.displayName?.isEmpty == false ? api.displayName! : "Guest reader")
                    .font(.headline)
                Text(api.isGoogleUser ? (api.userEmail ?? "")
                     : "Guest · sign in to sync across devices")
                    .font(.caption).foregroundStyle(pal.text2)
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
                    .foregroundStyle(pal.accent)
                Spacer()
                Button(ctx == nil ? "Set up" : "Edit") { showEdit = true }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(pal.accent)
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
                    .font(.footnote).foregroundStyle(pal.text2)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .blCard()
    }

    /// Appearance. Signed-in only, exactly as on the web: the choice is stored on
    /// the account so it carries to every device, which an anonymous local-only
    /// account can't honour.
    private var appearanceCard: some View {
        Button { if api.isGoogleUser { showTheme = true } else { showSignIn = true } } label: {
            HStack(spacing: 12) {
                Image(systemName: "paintpalette.fill")
                    .font(.title3).foregroundStyle(pal.accent)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Appearance")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                    Text(api.isGoogleUser
                         ? theme.skin.name
                         : "Sign in to choose a look and carry it across devices")
                        .font(.caption).foregroundStyle(pal.text2)
                        .multilineTextAlignment(.leading)
                }
                Spacer()
                SkinSwatch(skin: theme.skin)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(pal.text2)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .blCard()
        }
        .buttonStyle(.plain)
    }

    private var liveCard: some View {
        Button { showLiveConfig = true } label: {
            HStack(spacing: 12) {
                Image(systemName: "bolt.horizontal.circle.fill")
                    .font(.title3).foregroundStyle(pal.breaking)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Home live section")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                    Text(livePrefs.enabled
                         ? liveSummary : "Off — tap to enable breaking, scores & more")
                        .font(.caption).foregroundStyle(pal.text2)
                        .multilineTextAlignment(.leading)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(pal.text2)
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
                .foregroundStyle(pal.prediction)
            HStack {
                stat("flame.fill", "\(eng.streak)", "day streak", pal.warning)
                Divider().frame(height: 34).overlay(pal.hairline)
                stat("checkmark.seal.fill", "\(eng.understood)", "completed", pal.trust)
                Divider().frame(height: 34).overlay(pal.hairline)
                stat("safari.fill", "\(eng.topics.count)", "topics", pal.accent)
                Divider().frame(height: 34).overlay(pal.hairline)
                stat("bookmark.fill", "\(api.savedStoryIDs.count)", "saved", pal.prediction)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .blCard()
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.caption).foregroundStyle(pal.text2)
                .frame(width: 110, alignment: .leading)
            Text(value.isEmpty ? "—" : value).font(.footnote)
            Spacer(minLength: 0)
        }
    }

    private func stat(_ icon: String, _ value: String, _ label: String, _ color: Color) -> some View {
        VStack(spacing: 3) {
            Image(systemName: icon).font(.footnote).foregroundStyle(color)
            Text(value).font(.headline.monospaced())
            Text(label).font(.caption2).foregroundStyle(pal.text2)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Appearance

/// Three colour chips, the same swatch the web picker shows for each look.
struct SkinSwatch: View {
    let skin: DescrySkin
    var size: CGFloat = 26
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(skin.swatch.enumerated()), id: \.offset) { _, c in
                Rectangle().fill(c).frame(width: size / 3)
            }
        }
        .frame(height: size)
        .clipShape(RoundedRectangle(cornerRadius: pal.radius == 0 ? 0 : 6, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: pal.radius == 0 ? 0 : 6, style: .continuous)
            .stroke(pal.hairline, lineWidth: 1))
    }
}

/// The look picker. Selecting applies immediately (so the sheet itself repaints
/// in the chosen skin — the preview *is* the app), then persists to the account
/// so every other device picks it up.
struct ThemePickerView: View {
    @Environment(\.palette) private var pal
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject var api: APIClient
    @EnvironmentObject var theme: ThemeStore

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Same stories, your Descry. Switch anytime — this follows your account, not the device.")
                            .font(.footnote).foregroundStyle(pal.text2)
                            .padding(.bottom, 4)
                        ForEach(DescrySkin.allCases) { skin in
                            Button { choose(skin) } label: { row(skin) }
                                .buttonStyle(.plain)
                        }
                    }
                    .padding(18)
                }
                .scrollIndicators(.hidden)
            }
            .navigationTitle("Choose your look")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }.foregroundStyle(pal.accent)
                }
            }
        }
    }

    private func row(_ skin: DescrySkin) -> some View {
        let on = skin == theme.skin
        return HStack(spacing: 14) {
            SkinSwatch(skin: skin, size: 44)
            VStack(alignment: .leading, spacing: 3) {
                Text(skin.name).font(.subheadline.weight(.semibold)).foregroundStyle(.primary)
                Text(skin.blurb).font(.caption).foregroundStyle(pal.text2)
                    .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if on {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(pal.accent).font(.title3)
            }
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: pal.radius, style: .continuous)
            .fill(on ? pal.accent.opacity(0.08) : pal.surface))
        .overlay(RoundedRectangle(cornerRadius: pal.radius, style: .continuous)
            .stroke(on ? pal.accent : pal.hairline, lineWidth: 1))
    }

    private func choose(_ skin: DescrySkin) {
        // Apply locally first: the switch is instant and the sheet repaints in
        // the new skin, so the choice is its own preview. The write is the slow,
        // failable part and must not gate the feedback.
        withAnimation(BL.spring) { theme.set(skin) }
        Task {
            var ctx = UserDefaults.standard.data(forKey: "saved_context")
                .flatMap { try? JSONDecoder().decode(UserContext.self, from: $0) } ?? UserContext()
            ctx.theme = skin.rawValue
            try? await api.saveContext(ctx)
        }
    }
}
