import SwiftUI
import CoreLocation

/// One-shot location fetch + reverse geocode for onboarding autofill.
/// Requires NSLocationWhenInUseUsageDescription in Info.plist.
final class LocationOnce: NSObject, CLLocationManagerDelegate {
    static let shared = LocationOnce()
    private let manager = CLLocationManager()
    private var completion: ((CLPlacemark?) -> Void)?
    private var timeout: DispatchWorkItem?

    func request(_ done: @escaping (CLPlacemark?) -> Void) {
        completion = done
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer

        // Never leave the UI hanging: resolve as "not found" after 12s.
        timeout?.cancel()
        let t = DispatchWorkItem { [weak self] in self?.finish(nil) }
        timeout = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 12, execute: t)

        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()   // location requested on grant
        case .authorizedWhenInUse, .authorizedAlways:
            manager.requestLocation()
        default:
            finish(nil)                               // denied — user types it instead
        }
    }

    private func finish(_ placemark: CLPlacemark?) {
        DispatchQueue.main.async {
            self.timeout?.cancel()
            self.completion?(placemark)
            self.completion = nil                      // one-shot
        }
    }

    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        guard completion != nil else { return }
        switch m.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            m.requestLocation()
        case .denied, .restricted:
            finish(nil)
        default:
            break   // .notDetermined — waiting for the user's answer
        }
    }

    // Only ever invoked by CLLocationManager via delegate dispatch, never called
    // directly — marking it deprecated is safe (nothing else references it by
    // name) and lets it call the isolated legacy geocoder below without
    // re-surfacing the warning here.
    @available(*, deprecated, message: "delegate callback; calls the isolated legacy geocoder")
    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let loc = locs.first else { finish(nil); return }
        Self.legacyReverseGeocode(loc) { [weak self] placemark in
            self?.finish(placemark)
        }
    }

    func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        finish(nil)
    }

    /// CLGeocoder / reverseGeocodeLocation have no structured replacement as of
    /// iOS 26: the suggested MKReverseGeocodingRequest only returns
    /// MKAddressRepresentations (formatted address strings via fullAddress/
    /// shortAddress) — there is no supported way left to get discrete
    /// city/administrativeArea/country fields, which onboarding's location step
    /// and the backend's UserContext both require (confirmed via Apple DTS
    /// engineer response on the developer forums; open feedback FB20007974).
    /// Isolated in its own deprecated declaration so the warning is contained to
    /// one call instead of surfacing at every caller. Revisit if Apple ships a
    /// structured replacement.
    @available(*, deprecated, message: "isolates the CLGeocoder deprecation to this one call")
    private static func legacyReverseGeocode(_ location: CLLocation,
                                             completion: @escaping (CLPlacemark?) -> Void) {
        CLGeocoder().reverseGeocodeLocation(location) { placemarks, _ in
            completion(placemarks?.first)
        }
    }
}

/// "Calibrate your lens" — conversational, optional-everything context capture,
/// rebuilt with the Bluelligent Native design language.
struct OnboardingView: View {
    @EnvironmentObject var api: APIClient
    var initial: UserContext? = nil   // existing prefs to prefill when editing
    var onDone: () -> Void

    @State private var step = 0
    @State private var seeded = false
    @State private var ctx = UserContext()
    @State private var customInterest = ""
    @State private var microKey = ""
    @State private var microValue = ""
    @State private var saving = false
    @State private var error: String?
    @State private var locating = false
    @State private var locationNote: String?
    @State private var fields: [String] = []
    @State private var role: [String] = []
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let interestOptions = ["AI", "Technology", "Business", "World", "Science",
                                   "Health", "Energy", "Sports", "Politics",
                                   "Finance", "India"]
    private let fieldOptions = ["Software & IT", "Healthcare", "Finance", "Retail",
                                "Manufacturing", "Education", "Marketing & Sales",
                                "Engineering", "Legal", "Government", "Agriculture",
                                "Creative & Media", "Hospitality", "Logistics"]
    private let roleOptions = ["Student", "Employee", "Manager", "Executive",
                               "Business owner", "Freelancer", "Retired"]
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
        .onAppear(perform: seedFromExisting)
    }

    /// Prefill the form with the user's already-saved preferences (edit mode), so
    /// nothing is lost: the final PUT replaces the whole context, so we must start
    /// from the current values rather than a blank slate.
    private func seedFromExisting() {
        guard !seeded else { return }
        seeded = true
        guard let c = initial else { return }
        ctx = c
        fields = c.profession
            .split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { fieldOptions.contains($0) }
        role = roleOptions.filter { $0.lowercased() == c.roleSeniority.lowercased() }
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
                    .fill(i <= step ? AnyShapeStyle(BL.aiGradient) : AnyShapeStyle(BL.surface2))
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
        StepCard(title: "What's your world of work?",
                 subtitle: "Tap what fits — every story can then explain what it means for your work. Pick more than one if you like.") {
            Text("YOUR FIELD").font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.text2)
            FlowChips(options: fieldOptions, selected: $fields)
            Text("YOUR ROLE").font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.text2).padding(.top, 6)
            FlowChips(options: roleOptions, selected: $role)
            Text("ANYTHING SPECIFIC? (OPTIONAL)").font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.text2).padding(.top, 6)
            field("e.g. retail pharmacy chain, 3 stores", text: $ctx.lineOfBusiness)
        }
        .onChange(of: fields) { syncProfession() }
        .onChange(of: role) { syncProfession() }
    }

    private func syncProfession() {
        ctx.profession = fields.joined(separator: ", ")
        ctx.roleSeniority = role.first?.lowercased() ?? ""
    }

    private var locationStep: some View {
        StepCard(title: "Where are you?",
                 subtitle: "We link global trends to your city and region. One tap — or type it if you prefer.") {
            Button {
                // Self-diagnose the most common failure: without this Info.plist
                // key, iOS silently ignores the permission request (no popup).
                if Bundle.main.object(forInfoDictionaryKey: "NSLocationWhenInUseUsageDescription") == nil {
                    locationNote = "Setup needed: add “Privacy – Location When In Use Usage Description” to the app target's Info, then rebuild. iOS silently ignores location requests without it."
                    return
                }
                locating = true
                locationNote = nil
                LocationOnce.shared.request { placemark in
                    locating = false
                    if let p = placemark {
                        ctx.location.city = p.locality ?? p.subLocality
                            ?? p.subAdministrativeArea ?? ""
                        ctx.location.region = p.administrativeArea
                            ?? p.subAdministrativeArea ?? ""
                        ctx.location.country = p.country ?? ""
                    } else {
                        locationNote = "Couldn't detect automatically — you can type it below."
                    }
                }
            } label: {
                Label(locating ? "Detecting…" : "Use my current location",
                      systemImage: "location.fill")
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(.white)
                    .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(BL.aiGradient))
            }
            .disabled(locating)
            if let locationNote {
                Text(locationNote).font(.caption).foregroundStyle(BL.warning)
            }
            Text("Your precise location never leaves the phone — only the city name is saved.")
                .font(.caption2).foregroundStyle(BL.text2)
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
