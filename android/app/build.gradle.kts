plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "dev.battleroid.mobilegsscan"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.battleroid.mobilegsscan"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
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
    // ui-tooling is the heavyweight @Preview runtime — stays in
    // debugImplementation so it doesn't bloat the release APK.
    debugImplementation(libs.androidx.compose.ui.tooling)
}
