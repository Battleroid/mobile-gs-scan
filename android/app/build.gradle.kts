plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.kotlin.compose)
}

// Read the repo-wide ``VERSION`` file. Single source of truth for the
// semantic part of the app version on both Android and web; bumped
// manually before tagging a ``v*`` release. Wrapped in a try/catch so
// a missing file in unusual checkouts (no-history clone, CI cache miss)
// falls back to a known default rather than failing configuration.
val appBaseVersion: String = runCatching {
    rootProject.file("../VERSION").readText().trim()
}.getOrDefault("0.1.0")

// Short git SHA for the current HEAD. Appended to ``versionName`` as
// ``v0.1.0+ab12cd3`` for non-release builds so an installed APK
// reports exactly which commit produced it. Empty string when git
// isn't available (gradle sync from a tarball, sandboxed CI) — in
// that case versionName is just the base version, matching how a
// tagged release renders.
val appBuildSha: String = runCatching {
    val proc = ProcessBuilder("git", "rev-parse", "--short", "HEAD")
        .directory(rootProject.projectDir.parentFile)
        .redirectErrorStream(true)
        .start()
    proc.waitFor()
    proc.inputStream.bufferedReader().readText().trim()
}.getOrDefault("")

// Allow CI to override the suffix entirely (e.g. tagged release builds
// pass APP_BUILD_LABEL="" so the APK reads as a clean ``v0.1.0``). The
// "+sha" suffix is only added when there's a SHA to add AND no
// override is set.
val versionLabelOverride: String? = System.getenv("APP_BUILD_LABEL")
val computedVersionName: String = when {
    versionLabelOverride != null -> versionLabelOverride
    appBuildSha.isNotEmpty() -> "$appBaseVersion+$appBuildSha"
    else -> appBaseVersion
}

android {
    namespace = "dev.battleroid.mobilegsscan"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.battleroid.mobilegsscan"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = computedVersionName

        // Expose the version components to runtime code via
        // BuildConfig so the home header chip + Profile screen can
        // render them without parsing versionName back apart.
        buildConfigField(
            "String", "APP_BASE_VERSION", "\"$appBaseVersion\"",
        )
        buildConfigField(
            "String", "APP_BUILD_SHA", "\"$appBuildSha\"",
        )
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        debug {
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
        // Compose builds alongside the existing viewBinding XML
        // surface for now — PR-A introduces the runtime + theme;
        // later PRs migrate activities. Keeping viewBinding on means
        // every existing screen still compiles and runs unchanged.
        compose = true
    }
}

dependencies {
    implementation(libs.core.ktx)
    implementation(libs.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    implementation(libs.lifecycle.runtime.ktx)
    implementation(libs.preference.ktx)
    implementation(libs.recyclerview)
    implementation(libs.swiperefreshlayout)
    implementation(libs.arcore)
    implementation(libs.okhttp)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)

    // Compose UI — BOM-pinned. The BOM imports as a platform, then
    // the un-versioned androidx.compose.* artifacts inherit from it.
    // Single bump-the-BOM workflow keeps every compose dependency
    // in lockstep.
    val composeBom = platform(libs.androidx.compose.bom)
    implementation(composeBom)
    androidTestImplementation(composeBom)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.foundation)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.navigation.compose)
    // Coil — Compose AsyncImage for static thumbnails. Pulls in
    // coil-network-okhttp transitively; reuses OkHttp's HTTP cache
    // semantics so a flaky studio link doesn't manifest as torn
    // thumbnails.
    implementation(libs.coil.compose)
    // ui-tooling is the heavyweight @Preview runtime — stays in
    // debugImplementation so it doesn't bloat the release APK.
    debugImplementation(libs.androidx.compose.ui.tooling)
}
