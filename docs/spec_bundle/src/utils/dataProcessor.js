export async function loadAllData() {
  try {
    const response = await fetch('./raw_data/data.json');
    const jsonData = await response.json();
    return jsonData;
  } catch (error) {
    console.error('Error loading JSON data:', error);
    return {};
  }
}

export function calculateSpeedup(specValue, baselineValue) {
  if (!specValue || !baselineValue || baselineValue === 0) return null;
  return (specValue / baselineValue).toFixed(2);
}

export function processModelData(modelData, targetModelName) {
  if (!modelData || !targetModelName) return [];

  // Map to hold aggregated entries by unique key (draftModel + config)
  const entriesMap = new Map();

  // Iterate through each benchmark in the model
  Object.entries(modelData).forEach(([, benchmarkData]) => {
    const benchmarkName = benchmarkData.benchmark_name;
    const results = benchmarkData.results || [];

    results.forEach(result => {
      const { batch_size, steps, topk, num_draft_tokens, metrics } = result;

      // Find baseline (Without EAGLE3)
      const baselineMetric = metrics.find(m => m.Name === 'Wihtout EAGLE3');

      // Process each metric entry (including baseline and EAGLE3 models)
      metrics.forEach(metric => {
        const isBaseline = metric.Name === 'Wihtout EAGLE3';
        const config = isBaseline ? 'baseline' : `${batch_size}-${steps}-${topk}-${num_draft_tokens}`;

        // draftModel is the Name from metrics array
        const draftModel = isBaseline ? 'None' : metric.Name;

        // Use a combination of draftModel and config as the key
        // This ensures baseline and EAGLE3 configs are separate entries
        const key = `${draftModel}|${config}`;

        // Get or create entry
        if (!entriesMap.has(key)) {
          entriesMap.set(key, {
            targetModel: targetModelName,
            draftModel: draftModel,
            config,
            batch_size,
            steps,
            topk,
            num_draft_tokens,
            metrics: {},
            baseline: {}
          });
        }

        const entry = entriesMap.get(key);

        // Add this benchmark's metrics
        entry.metrics[benchmarkName] = {
          throughput: metric.output_throughput,
          accLen: metric.accept_length
        };

        // Add baseline for this benchmark
        if (baselineMetric) {
          entry.baseline[benchmarkName] = {
            throughput: baselineMetric.output_throughput,
            accLen: baselineMetric.accept_length
          };
        }
      });
    });
  });

  return Array.from(entriesMap.values());
}

export function getTargetModels(allData) {
  return Object.keys(allData);
}

export function extractUniqueTargetModels(processedData) {
  return [...new Set(processedData.map(d => d.targetModel).filter(Boolean))];
}
