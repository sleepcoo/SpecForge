import * as XLSX from 'xlsx';

export const modelFiles = {
  'Qwen3': './raw_data/SpecBundle-Qwen3.xlsx',
  'Llama': './raw_data/SpecBundle-Llama.xlsx'
};

export async function loadXLSX(filePath) {
  try {
    const response = await fetch(filePath);
    const arrayBuffer = await response.arrayBuffer();
    const workbook = XLSX.read(arrayBuffer, { type: 'array' });
    const sheetName = workbook.SheetNames[0];
    // header: 1 returns an array of arrays, which mimics our previous CSV parsing output
    const rawData = XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { header: 1, defval: '' });
    return rawData;
  } catch (error) {
    console.error(`Error loading Excel from ${filePath}:`, error);
    return [];
  }
}

export async function loadAllData() {
  const allData = {};

  for (const [modelFamily, filePath] of Object.entries(modelFiles)) {
    allData[modelFamily] = await loadXLSX(filePath);
  }

  return allData;
}

export function calculateSpeedup(specValue, baselineValue) {
  if (!specValue || !baselineValue || baselineValue === 0) return null;
  return (specValue / baselineValue).toFixed(2);
}

function findColumnIndices(headerRow) {
  const colMap = {
    target: -1,
    draft: -1,
    parallel: -1,
    eagle: -1,
    hardware: -1
  };

  const benchmarks = {}; // name -> startIdx

  headerRow.forEach((col, idx) => {
    if (!col) return;
    const text = col.trim().toLowerCase();

    // Standardize column matching
    if (text.includes('target model')) colMap.target = idx;
    else if (text.includes('draft model')) colMap.draft = idx;
    else if (text.includes('parallel config')) colMap.parallel = idx;
    else if (text.includes('eagle3 config')) colMap.eagle = idx;
    else if (text.includes('hardware')) colMap.hardware = idx;
    else {
      const benchmarkName = ['MTBench', 'HumanEval', 'GSM8K', 'Math500'].find(b => text.includes(b.toLowerCase()));
      if (benchmarkName) {
        benchmarks[benchmarkName] = idx;
      }
    }
  });

  // Heuristic for Hardware if missing (Spec-Bundle Llama.csv case)
  // If we have Draft and Parallel, and there is a gap of 1 in between
  if (colMap.hardware === -1 && colMap.draft !== -1 && colMap.parallel !== -1) {
    if (colMap.parallel - colMap.draft === 2) {
      colMap.hardware = colMap.draft + 1;
    }
  }

  return { colMap, benchmarks };
}

export function processModelData(rawData) {
  if (!rawData || rawData.length < 3) return [];

  const headers = rawData[0];
  const { colMap, benchmarks } = findColumnIndices(headers);

  const processed = [];
  let currentTarget = null;
  let currentBaselineMetrics = null;
  let currentDraftModel = null; // Track current draft model for inheritance

  // Start from row 2 (skipping header and sub-header)
  // Note: Row indices might vary depending on empty lines, but Papa parse with skipEmptyLines helps.
  // However, inconsistent files might be tricky. Let's assume data starts at row 2 usually.

  // Actually, we should iterate and check content.
  // First valid data row is usually row 2 (if 0-based).

  for (let i = 2; i < rawData.length; i++) {
    const row = rawData[i];
    if (!row || row.length === 0) continue;

    const rawTarget = colMap.target !== -1 ? row[colMap.target] : null;
    if (rawTarget && rawTarget.trim()) {
      currentTarget = rawTarget.trim().replace(/\n/g, ' '); // Handle multi-line target names
      currentBaselineMetrics = null; // New target group, reset baseline
      currentDraftModel = null; // Reset draft model for new target
    }

    if (!currentTarget) continue;

    // Get draft model from current row, or inherit from previous row
    const rawDraftModel = colMap.draft !== -1 ? row[colMap.draft] : null;
    if (rawDraftModel && rawDraftModel.trim()) {
      currentDraftModel = rawDraftModel.trim();
    }
    const draftModel = currentDraftModel || '-';

    // Configs
    const parallelConfig = colMap.parallel !== -1 && row[colMap.parallel]?.trim() ? row[colMap.parallel].trim() : '-';
    const eagleConfig = colMap.eagle !== -1 && row[colMap.eagle]?.trim() ? row[colMap.eagle].trim() : '-';

    // Skip rows that are just commands or empty structural rows
    // Heuristic: Must have at least one valid metric or be a valid config row

    const metrics = {};
    let hasMetrics = false;

    Object.entries(benchmarks).forEach(([name, idx]) => {
      // In the new format, Acc Len is at idx, Throughput is at idx + 1
      const accLenStr = row[idx];
      const throughputStr = row[idx + 1];

      // Parse with cleanup for "token/s"
      const accLen = parseFloat(accLenStr);
      let throughput = 0;

      if (throughputStr) {
        // Remove 'token/s', commas, whitespace
        const cleanStr = throughputStr.toString().toLowerCase().replace('token/s', '').replace(/,/g, '').trim();
        throughput = parseFloat(cleanStr);
      }

      const isValidAcc = !isNaN(accLen);
      const isValidTp = !isNaN(throughput) && throughput > 0;

      if (isValidAcc || isValidTp) {
        metrics[name] = {
          accLen: isValidAcc ? accLen : null,
          throughput: isValidTp ? throughput : null
        };
        hasMetrics = true;
      }
    });

    // We used to skip rows if !hasMetrics, but this hides Target Models that might exist
    // in the file but have no data yet (placeholders).
    // We should include them so they appear in the filter.
    // However, we still want to skip completely empty infrastructure rows if they don't even have a valid draft model.
    // If it has a draft model (even '-'), we keep it.

    // if (!hasMetrics) continue; // REMOVED strict check

    // Identify Baseline
    // Baseline logic: draftModel is '-' OR eagleConfig is '0-0-0'
    const isBaseline = (draftModel === '-' || draftModel === 'None' || draftModel === '') || (eagleConfig === '0-0-0');

    if (isBaseline) {
      currentBaselineMetrics = metrics;
    }

    const entry = {
      targetModel: currentTarget,
      draftModel: (draftModel === '-' || draftModel === '') ? 'None' : draftModel,
      config: eagleConfig !== '-' && eagleConfig !== '' ? eagleConfig : parallelConfig,
      metrics,
      baseline: currentBaselineMetrics
    };

    processed.push(entry);
  }

  // Post-process to ensure rows that came AFTER the baseline in the same group also get that baseline
  // (Since we set currentBaselineMetrics when we encounter it, rows *after* it get it assigned.
  // But if baseline is the 2nd row for some reason, 1st row wouldn't get it?
  // Usually baseline is first. But let's be robust: groupings by Target Model)

  const byTarget = {};
  processed.forEach(p => {
    if (!byTarget[p.targetModel]) byTarget[p.targetModel] = [];
    byTarget[p.targetModel].push(p);
  });

  const finalProcessed = [];
  Object.values(byTarget).forEach(group => {
    // Find baseline in this group
    const baselineEntry = group.find(p => p.draftModel === 'None' || p.config === '0-0-0');
    const baseline = baselineEntry ? baselineEntry.metrics : null;

    group.forEach(item => {
      item.baseline = baseline;
      finalProcessed.push(item);
    });
  });

  return finalProcessed;
}

export function getModelFamilies(allData) {
  return Object.keys(allData);
}

export function extractUniqueTargetModels(processedData) {
  return [...new Set(processedData.map(d => d.targetModel).filter(Boolean))];
}
