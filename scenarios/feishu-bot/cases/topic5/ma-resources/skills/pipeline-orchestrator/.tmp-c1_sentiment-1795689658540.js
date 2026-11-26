
const W = 800;
const H = 520;
const BASE_W = 1200;
const BASE_H = 600;
const scale = Math.min(W / BASE_W, H / BASE_H);
const FONT_MAIN = Math.max(13, Math.round(18 * scale));
const FONT_SMALL = Math.max(13, Math.round(14 * scale));
const LEGEND_ICON = Math.max(8, Math.round(10 * scale));

function mixWhite(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgb(${Math.round(r * 0.6 + 255 * 0.4)}, ${Math.round(g * 0.6 + 255 * 0.4)}, ${Math.round(b * 0.6 + 255 * 0.4)})`;
}

function compactCount(value) {
  if (value >= 100000000) return `${(value / 100000000).toFixed(1)}亿`;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return `${Math.round(value)}`;
}

function percentLabel(value) {
  const rounded = Math.round(value * 10) / 10;
  return rounded % 1 === 0 ? `${rounded.toFixed(0)}%` : `${rounded.toFixed(1)}%`;
}


const data = [
  {
    "name": "正面",
    "value": 18,
    "color": "#5DBB7D"
  },
  {
    "name": "中性",
    "value": 7,
    "color": "#AAAAAA"
  },
  {
    "name": "负面",
    "value": 5,
    "color": "#F06060"
  }
].map((item) => ({
  ...item,
  itemStyle: {
    borderColor: "#FFFFFF",
    borderWidth: 3,
    color: new echarts.graphic.LinearGradient(0, 0, 1, 1, [
      { offset: 0, color: item.color },
      { offset: 1, color: mixWhite(item.color) }
    ])
  }
}));

var option = {
  backgroundColor: "#FFFFFF",
  animation: false,
  tooltip: {
    trigger: "item",
    backgroundColor: "#1D2129",
    borderWidth: 0,
    padding: 12,
    textStyle: { color: "#FFFFFF", fontSize: FONT_MAIN },
    formatter: (params) => `${params.name}<br/>占比：${params.value.toFixed(2)}%`
  },
  legend: {
    bottom: 8,
    icon: "circle",
    itemWidth: LEGEND_ICON,
    itemHeight: LEGEND_ICON,
    textStyle: { color: "#4E5969", fontSize: FONT_MAIN }
  },
  series: [
    {
      type: "pie",
      radius: ["40%", "68%"],
      center: ["50%", "45%"],
      avoidLabelOverlap: true,
      minAngle: 4,
      label: {
        show: true,
        formatter: "{b}\n{d}%",
        alignTo: "edge",
        edgeDistance: "12%",
        color: "#4E5969",
        fontSize: FONT_MAIN,
        lineHeight: Math.round(FONT_MAIN * 1.35)
      },
      labelLine: {
        show: true,
        length: 16,
        length2: 12,
        smooth: 0.2
      },
      itemStyle: {
        borderColor: "#FFFFFF",
        borderWidth: 3
      },
      data
    }
  ]
};
