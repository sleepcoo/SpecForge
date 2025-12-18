<template>
  <div class="benchmark-app">
    <!-- Main Content Container -->
    <div class="main-container">

      <!-- Header / Hero -->
      <header class="header">
        <div class="header-content">
          <div class="brand">
            <div class="logo-icon">
              <img src="../assets/specforge-logo.png" alt="SpecForge Logo" style="width: 100%; height: 100%; object-fit: contain;">
            </div>
          </div>
          <h1>SpecBundle</h1>
        </div>
      </header>

      <!-- Dashboard Controls -->
      <div class="dashboard-controls">
        <FilterControls
          :modelFamilies="modelFamilies"
          :selectedFamily="selectedFamily"
          :targetModels="uniqueModels"
          :selectedTargetModel="selectedTargetModel"
          :benchmarks="benchmarks"
          :selectedBenchmark="selectedBenchmark"
          :metrics="metricOptions"
          :selectedMetric="selectedMetric"
          @update:family="selectedFamily = $event"
          @update:targetModel="selectedTargetModel = $event"
          @update:benchmark="selectedBenchmark = $event"
          @update:metric="selectedMetric = $event"
        />
      </div>

      <!-- Navigation Tabs Removed per user request -->
      <!-- The Model Family selection is now handled securely by the FilterControls dropdown above. -->

      <!-- Loading State -->
      <div v-if="loading" class="state-container">
        <div class="spinner"></div>
        <p>Loading benchmark data...</p>
      </div>

      <!-- Content Display -->
      <div v-else-if="currentData.length > 0" class="content-wrapper">
        <div class="stats-overview">
          <div class="stat-card">
            <span class="stat-label">Model Family</span>
            <span class="stat-value">{{ selectedFamily }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Target Models</span>
            <span class="stat-value">{{ uniqueModels.length }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Configurations</span>
            <span class="stat-value">{{ currentData.length }}</span>
          </div>
        </div>

        <div class="chart-section">
          <h3>Performance Visualization</h3>
          <div class="chart-container">
            <BenchmarkChart
              :data="currentData"
              :benchmark="selectedBenchmark"
              :metric="selectedMetric"
              :modelFamily="selectedFamily"
            />
          </div>
        </div>

        <div class="table-section">
          <div class="section-header">
            <h3>Detailed Results</h3>
            <div class="export-actions">
              <!-- Feature placeholder for Export -->
            </div>
          </div>
          <BenchmarkTable
            :data="currentData"
            :benchmarks="benchmarks"
            :selectedBenchmark="selectedBenchmark"
            :highlightMetric="selectedMetric"
          />
        </div>
      </div>



      <!-- No Data State -->
      <div v-else class="state-container empty">
        <div class="empty-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
        </div>
        <h3>No Data Available</h3>
        <p>No benchmark data found for {{ selectedFamily }}</p>
      </div>

    </div>

    <footer class="footer">
      <p>Powered by SpecForge Team</p>
    </footer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue';
import FilterControls from './FilterControls.vue';
import BenchmarkChart from './BenchmarkChart.vue';
import BenchmarkTable from './BenchmarkTable.vue';
import {
  loadAllData,
  processModelData,
  getModelFamilies,
  extractUniqueTargetModels
} from '../utils/dataProcessor';

const loading = ref(true);
const allData = ref({});
const allProcessedData = ref({});
const selectedFamily = ref('GPT-OSS');
const selectedTargetModel = ref('all');
const selectedBenchmark = ref('all');
const selectedMetric = ref('throughput');

// Fixed Order for Metrics
const benchmarks = ['MTBench', 'HumanEval', 'GSM8K', 'Math500'];
const metricOptions = [
  { value: 'throughput', label: 'Throughput (tokens/s)' },
  { value: 'accLen', label: 'Acceptance Length' },
  { value: 'speedup', label: 'Speedup vs Baseline' }
];

const modelFamilies = computed(() => getModelFamilies(allData.value));

const uniqueModels = computed(() => {
  const familyData = allProcessedData.value[selectedFamily.value] || [];
  return extractUniqueTargetModels(familyData);
});

const currentData = computed(() => {
  const familyData = allProcessedData.value[selectedFamily.value] || [];

  if (selectedTargetModel.value && selectedTargetModel.value !== 'all') {
    return familyData.filter(d => d.targetModel === selectedTargetModel.value);
  }

  return familyData;
});

// Update target model selection when family changes
watch([selectedFamily, uniqueModels], ([newFamily, newModels]) => {
    if (newModels && newModels.length > 0) {
        // Default to the first target model to avoid mixing data
        if (!newModels.includes(selectedTargetModel.value)) {
             selectedTargetModel.value = newModels[0];
        }
    } else {
        selectedTargetModel.value = 'all';
    }
}, { immediate: true });

onMounted(async () => {
  try {
    allData.value = await loadAllData();

    for (const [family, data] of Object.entries(allData.value)) {
      allProcessedData.value[family] = processModelData(data);
    }

    // Set default family if available
    const families = Object.keys(allData.value);
    if (families.length > 0 && !families.includes(selectedFamily.value)) {
      selectedFamily.value = families[0];
    }

    // Initial target model selection will be handled by the watch

    loading.value = false;
  } catch (error) {
    console.error('Error loading data:', error);
    loading.value = false;
  }
});
</script>

<style scoped>
.benchmark-app {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.main-container {
  max-width: 1440px;
  margin: 0 auto;
  padding: 40px 24px;
  width: 100%;
  flex: 1;
}

/* Header */
.header {
  margin-bottom: 40px;
  text-align: center;
}

.header-content {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: center;
  gap: 1.25rem;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo-icon {
  width: auto;
  height: 80px; /* Enlarged logo for better visual presence */
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-name {
  font-family: 'Space Grotesk', sans-serif; /* Modern tech/space font */
  font-weight: 700;
  font-size: 2.2rem;
  color: var(--color-text-main);
  letter-spacing: -0.03em; /* Tighter tracking for Grotesk look */
}

h1 {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 2.5rem;
  line-height: 1;
  background: linear-gradient(135deg, var(--color-text-main) 0%, var(--color-primary) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin: 0;
  letter-spacing: -0.02em;
}

.subtitle {
  font-size: 1.25rem;
  color: var(--color-text-secondary);
  max-width: 600px;
  text-align: center;
}

/* Dashboard Controls */
.dashboard-controls {
  margin-bottom: 32px;
}

/* Tabs */
.tabs-container {
  margin-bottom: 40px;
  display: flex;
  justify-content: center;
}

.tabs-scroll {
  display: flex;
  gap: 8px;
  padding: 8px;
  background: white;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow-x: auto;
  max-width: 100%;
  border: 1px solid #e2e8f0;
}

.tab-btn {
  padding: 10px 20px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-weight: 600;
  font-size: 0.9375rem;
  border-radius: var(--radius-lg);
  transition: all 0.2s ease;
  white-space: nowrap;
}

.tab-btn:hover {
  color: var(--color-primary);
  background: #f1f5f9;
}

.tab-btn.active {
  background: var(--color-text-main);
  color: white;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.2);
}

.tab-divider {
  width: 1px;
  background: #e2e8f0;
  margin: 4px 8px;
}

.tab-btn.comparison {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-primary);
}

.tab-btn.comparison.active {
  background: var(--color-primary);
  color: white;
  box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
}

/* Content */
.content-wrapper {
  animation: fadeIn 0.5s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

/* Stats Overview */
.stats-overview {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 24px;
  margin-bottom: 32px;
}

.stat-card {
  background: white;
  padding: 24px;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm), inset 0 1px 0 0 rgba(255, 255, 255, 0.6);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  border: 1px solid rgba(255, 255, 255, 0.5); /* Subtle border mostly for blend */
  transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), box-shadow 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
  overflow: hidden;
}

/* Removed ::after (Blue Line) per user request */

.stat-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-xl);
}

/* Text color change on hover (Blue) */
.stat-card:not(.highlight):hover .stat-value,
.stat-card:not(.highlight):hover .stat-label {
  color: var(--color-primary);
}

.stat-card.highlight {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  box-shadow: var(--shadow-glow);
  border: none;
}

.stat-card.highlight .stat-label {
  color: rgba(255,255,255, 0.8);
}

.stat-card.highlight .stat-value {
  color: white;
}

.stat-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--color-text-muted);
  font-weight: 600;
  margin-bottom: 8px;
  transition: color 0.3s ease;
}

.stat-value {
  font-family: var(--font-display);
  font-size: 2.25rem;
  font-weight: 700;
  color: var(--color-text-main);
  line-height: 1;
  letter-spacing: -0.02em;
  transition: color 0.3s ease;
}

/* Sections */
.chart-section,
.table-section {
  background: white;
  border-radius: var(--radius-2xl);
  box-shadow: var(--shadow-lg), inset 0 1px 0 0 rgba(255, 255, 255, 0.6);
  padding: 40px; /* Increased padding */
  margin-bottom: 32px;
  border: 1px solid rgba(255, 255, 255, 0.5); /* Very subtle */
}

.chart-section h3,
.section-header h3 {
  font-family: var(--font-display);
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--color-text-main);
  margin-bottom: 24px;
}

.chart-container {
  width: 100%;
  height: 500px; /* Reduced height for better fit */
}

/* States */
.state-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 0;
  text-align: center;
}

.spinner {
  width: 50px;
  height: 50px;
  border: 4px solid #e2e8f0;
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-bottom: 24px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.empty-icon {
  margin-bottom: 24px;
  color: var(--color-text-muted);
}

/* Footer */
.footer {
  margin-top: auto;
  padding: 40px 0;
  text-align: center;
  color: var(--color-text-muted);
  font-size: 0.875rem;
  border-top: 1px solid #e2e8f0;
}

/* Responsive */
@media (max-width: 768px) {
  h1 { font-size: 2rem; }
  .main-container { padding: 20px 16px; }
  .chart-section, .table-section { padding: 20px; }
  .tabs-scroll { flex-wrap: wrap; }
}
</style>
