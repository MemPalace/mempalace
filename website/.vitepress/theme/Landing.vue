<script setup>
import { useLandingEffects } from './landing/useLandingEffects.js'
import FolioHeader from './landing/FolioHeader.vue'
import HeroSection from './landing/HeroSection.vue'
import ForgettingSection from './landing/ForgettingSection.vue'
import AnatomySection from './landing/AnatomySection.vue'
import DialectSection from './landing/DialectSection.vue'
import MechanicsSection from './landing/MechanicsSection.vue'
import InstallSection from './landing/InstallSection.vue'
import CatalogFooter from './landing/CatalogFooter.vue'
import './landing/landing.css'

useLandingEffects()
</script>

<template>
  <div class="mempalace-landing">
    <div class="page">
      <FolioHeader />
      <HeroSection />
      <ForgettingSection />
      <AnatomySection />
      <DialectSection />
      <MechanicsSection />
      <InstallSection />
      <CatalogFooter />
    </div>
  </div>
</template>
