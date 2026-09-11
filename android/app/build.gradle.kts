plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.bodish.niyoj"
    compileSdk = 35

    defaultConfig {
        // the id the device and Play know it by; the Kotlin package stays
        // com.* because `in` is a Kotlin keyword and would need backticks
        applicationId = "in.bodish.niyoj"
        minSdk = 26                  // java.util.Base64 + the TLS stack sshj expects
        targetSdk = 35
        versionCode = 10119
        versionName = "1.1.19"
    }
    buildFeatures { buildConfig = true }
    signingConfigs {
        create("sideload") {
            val ks = rootProject.file("niyoj.keystore")   // gitignored; see README
            if (ks.exists()) {
                storeFile = ks
                keyAlias = "niyoj"
                storePassword = providers.gradleProperty("niyojKeystorePass").orNull.orEmpty()
                keyPassword = storePassword
            }
        }
    }
    buildTypes {
        release {
            // ponytail: no shrinking — sshj+BouncyCastle need keep rules we would
            // have to chase on every bump, and the apk is small enough as is.
            isMinifyEnabled = false
            // no keystore yet? still produce an installable apk rather than an
            // unsigned one nobody can sideload
            signingConfig = signingConfigs.getByName(
                if (rootProject.file("niyoj.keystore").exists()) "sideload" else "debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
    packaging {
        resources.excludes += setOf(
            "META-INF/*.SF", "META-INF/*.DSA", "META-INF/*.RSA",
            "META-INF/DEPENDENCIES", "META-INF/INDEX.LIST",
            "META-INF/LICENSE*", "META-INF/NOTICE*", "META-INF/versions/**",
        )
    }
}

// The desktop UI is the phone UI. Copy it in at build time so there is never a
// second copy to keep in sync; scripts.json comes from `python deployer.py --scripts`.
val copyUi by tasks.registering(Copy::class) {
    from(rootProject.file("../ui.html"))
    from(rootProject.file("../bootstrap-icons.woff2"))
    into(layout.projectDirectory.dir("src/main/assets"))
}
tasks.named("preBuild") { dependsOn(copyUi) }

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.hierynomus:sshj:0.39.0")
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
    implementation("org.slf4j:slf4j-nop:1.7.36")     // sshj logs through slf4j; silence it
    testImplementation("junit:junit:4.13.2")
    // sshj's transitives are runtime-only on the test classpath otherwise, and the
    // key round-trip test needs to actually parse an ed25519 key
    testImplementation("net.i2p.crypto:eddsa:0.3.0")
    testImplementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
}
