import SwiftUI

/// "Calibrate your lens" — conversational, optional-everything context capture,
/// rebuilt with the Bluelligent Native design language.
struct OnboardingView: View {
    @EnvironmentObject var api: APIClient
    var onDone: () -> Void

    @State private var step = 0
    @State private var ctx = UserContext()
    @State private var customInterest = ""
    @State private var microKey = ""
    @State private var microValue = ""
    @State private var saving = false
    @State private var error: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let interestOptions = ["Technology", "Business", "World", "Science",
                                   "Health", "Energy", "Sports", "Politics",
                                   "Finance", "India"]
    private let seniorities = ["Student", "Employee", "Manager", "Executive", "Owner"]
    private let languages = ["English", "Hindi", "Marathi", "Tamil", "Telugu",
                             "Bengali", "Spanish", "French", "German", "Other"]
    private let totalSteps = 6

    var body: some View {
        ZStack {
            InkBackground()
            orbs
            VStack(spacing: 0) {
                progressDots
                TabView(selection: $step) {
                    interestsStep.tag(0)
                    professionStep.tag(1)
                    locationStep.tag(2)
                    languageStep.tag(3)
                    microStep.tag(4)
                    reviewStep.tag(5)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .animation(BL.spring, value: step)
                controls
            }
        }
        .preferredColorScheme(.dark)
    }

    // MARK: - Ambient orbs (skip when reduce-motion)

    @State private var drift = false
    private var orbs: some View {
        ZStack {
            Circle().fill(BL.accent.opacity(0.18)).frame(width: 260)
                .blur(radius: 70)
                .offset(x: drift ? 90 : -60, y: drift ? -180 : -120)
            Circle().fill(BL.ai.opacity(0.15)).frame(width: 300)
                .blur(radius: 80)
                .offset(x: drift ? -80 : 60, y: drift ? 260 : 200)
        }
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 9).repeatForever(autoreverses: true)) {
                drift = true
            }
        }
        .accessibilityHidden(true)
    }

    private var progressDots: some View {
        HStack(spacing: 7) {
            ForEach(0..<totalSteps, id: \.self) { i in
                Capsule()
                    .fill(i <= step ? AnyShapeStyle(BL.aiGradient) : AnyShapeStyle(Color.white.opacity(0.12)))
                    .frame(width: i == step ? 22 : 7, height: 7)
                    .animation(BL.spring, value: step)
            }
        }
        .padding(.top, 18)
        .accessibilityLabel("Step \(step + 1) of \(totalSteps)")
    }

    // MARK: - Steps

    private var interestsStep: some View {
        StepCard(title: "What do you care about?",
                 subtitle: "Pick topics you want to follow. This shapes your daily brief.") {
            FlowChips(options: interestOptions, selected: $ctx.interests)
            HStack {
                field("Add your own…", text: $customInterest)
                Button("Add") {
                    let t = customInterest.trimmingCharacters(in: .whitespaces)
                    if !t.isEmpty, !ctx.interests.contains(t) { ctx.interests.append(t) }
                    customInterest = ""
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(BL.accent)
            }
        }
    }

    private var professionStep: some View {
        StepCard(title: "What do you do?",
                 subtitle: "So every story can explain what it means for your work.") {
            field("Profession (e.g. pharmacist)", text: $ctx.profession)
            field("Line of business (e.g. retail pharmacy, 3 stores)", text: $ctx.lineOfBusiness)
            Picker("Role", selection: $ctx.roleSeniority) {
                Text("Prefer not to say").tag("")
                ForEach(seniorities, id: \.self) { Text($0).tag($0.lowercased()) }
            }
            .pickerStyle(.segmented)
        }
    }

    private var locationStep: some View {
        StepCard(title: "Where are you?",
                 subtitle: "We link global trends to your city and region.") {
            field("City", text: $ctx.location.city)
            field("State / Region", text: $ctx.location.region)
            field("Country", text: $ctx.location.country)
        }
    }

    private var languageStep: some View {
        StepCard(title: "Languages",
                 subtitle: "We can render summaries in your native language.") {
            Picker("Native language", selection: $ctx.nativeLanguage) {
                Text("Select…").tag("")
                ForEach(languages, id: \.self) { Text($0).tag($0) }
            }
            .pickerStyle(.wheel)
            .frame(maxHeight: 150)
            Toggle("Read news in English", isOn: Binding(
                get: { ctx.preferredLanguage == "English" },
                set: { ctx.preferredLanguage = $0 ? "English" : ctx.nativeLanguage }))
                .tint(BL.accent)
        }
    }

    private var microStep: some View {
        StepCard(title: "The details that make it personal",
                 subtitle: "Anything news could touch — commute, investments, kids' school, supply chains, goals.") {
            ForEach(Array(ctx.micro.keys.sorted()), id: \.self) { key in
                HStack {
                    Text(key).font(.caption).foregroundStyle(BL.text2)
                    Text(ctx.micro[key] ?? "").font(.footnote)
                    Spacer()
                    Button(role: .destructive) { ctx.micro.removeValue(forKey: key) }
                    label: { Image(systemName: "xmark.circle.fill").foregroundStyle(BL.text2) }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(BL.surface2))
            }
            HStack {
                field("e.g. commute", text: $microKey)
                field("e.g. drives 40 min daily", text: $microValue)
                Button("Add") {
                    let k = microKey.trimmingCharacters(in: .whitespaces)
                    let v = microValue.trimmingCharacters(in: .whitespaces)
                    if !k.isEmpty, !v.isEmpty { ctx.micro[k] = v }
                    microKey = ""; microValue = ""
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(BL.accent)
            }
        }
    }

    private var reviewStep: some View {
        StepCard(title: "Your news lens",
                 subtitle: "This is the context we use to explain what each story means for you. Edit any time.") {
            Group {
                row("Interests", ctx.interests.joined(separator: ", "))
                row("Profession", ctx.profession)
                row("Business", ctx.lineOfBusiness)
                row("Location", [ctx.location.city, ctx.location.region, ctx.location.country]
                    .filter { !$0.isEmpty }.joined(separator: ", "))
                row("Native language", ctx.nativeLanguage)
                row("Personal details", ctx.micro.map { "\($0.key): \($0.value)" }
                    .joined(separator: " · "))
            }
            if let error {
                Text(error).foregroundStyle(BL.breaking).font(.caption)
            }
        }
    }

    // MARK: - Pieces

    private func field(_ placeholder: String, text: Binding<String>) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain)
            .padding(.horizontal, 14).padding(.vertical, 11)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(BL.surface2))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(BL.hairline, lineWidth: 1))
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.caption).foregroundStyle(BL.text2)
                .frame(width: 110, alignment: .leading)
            Text(value.isEmpty ? "—" : value).font(.callout)
            Spacer()
        }
        .padding(.vertical, 2)
    }

    private var controls: some View {
        HStack {
            if step > 0 {
                Button("Back") { withAnimation(BL.spring) { step -= 1 } }
                    .foregroundStyle(BL.text2)
            }
            Spacer()
            if step < totalSteps - 1 {
                Button("Skip") { withAnimation(BL.spring) { step += 1 } }
                    .foregroundStyle(BL.text2)
                    .padding(.trailing, 8)
                Button {
                    withAnimation(BL.spring) { step += 1 }
                } label: {
                    Text("Next")
                        .font(.subheadline.weight(.semibold))
                        .padding(.horizontal, 24).padding(.vertical, 12)
                        .foregroundStyle(.white)
                        .background(Capsule().fill(BL.aiGradient))
                }
            } else {
                Button {
                    Task {
                        saving = true
                        do {
                            try await api.saveContext(ctx)
                            onDone()
                        } catch {
                            self.error = "Couldn't reach the server. Is the backend running?"
                        }
                        saving = false
                    }
                } label: {
                    Text(saving ? "Calibrating…" : "Start understanding")
                        .font(.subheadline.weight(.semibold))
                        .padding(.horizontal, 24).padding(.vertical, 12)
                        .foregroundStyle(.white)
                        .background(Capsule().fill(BL.aiGradient))
                        .shadow(color: BL.ai.opacity(0.4), radius: 12, y: 5)
                }
                .disabled(saving)
            }
        }
        .padding(20)
    }
}

// MARK: - Reusable pieces

struct StepCard<Content: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder var content: Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(title)
                    .font(.system(.largeTitle, design: .serif, weight: .semibold))
                Text(subtitle).foregroundStyle(BL.text2)
                content
            }
            .padding(22)
        }
        .scrollIndicators(.hidden)
    }
}

struct FlowChips: View {
    let options: [String]
    @Binding var selected: [String]

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104))], spacing: 8) {
            ForEach(options, id: \.self) { opt in
                let isOn = selected.contains(opt)
                Button {
                    withAnimation(BL.spring) {
                        if isOn { selected.removeAll { $0 == opt } }
                        else { selected.append(opt) }
                    }
                } label: {
                    Text(opt)
                        .font(.footnote.weight(.medium))
                        .padding(.vertical, 9)
                        .frame(maxWidth: .infinity)
                        .background(Capsule().fill(isOn ? BL.accent.opacity(0.2) : BL.surface2))
                        .overlay(Capsule().stroke(isOn ? BL.accent.opacity(0.5) : BL.hairline,
                                                  lineWidth: 1))
                        .foregroundStyle(isOn ? BL.accent : .white)
                }
                .sensoryFeedback(.selection, trigger: isOn)
            }
        }
    }
}
