# Knowledge Graph Visualization Page

## Overview

A new interactive knowledge graph visualization page has been added to the Descry web portal. This page allows users to explore financial relationships, see how companies and entities are connected, and navigate to related stories.

## Accessing the Page

The knowledge graph page is available at:
```
https://descry.in/graph.html
```

Or on your local development server:
```
http://localhost:3000/graph.html
```

## Features

### 1. **Interactive Graph Visualization**
   - Visual representation of financial entities (companies, regulators, sectors, etc.)
   - Node size represents entity prominence (mentions in news)
   - Color-coded by entity type:
     - 🔴 Regulators (red)
     - 🔵 Organizations/Companies (blue)
     - 🟢 Sectors (green)
     - 🟠 Geographies (orange)
     - 🟣 Other entity types (purple)

### 2. **Entity Details Panel**
   - Click any node to view:
     - Entity name and type
     - Number of mentions in the news
     - Total connections in the graph
     - Direct relationships (connections) with confidence scores
     - Related stories mentioning the entity

### 3. **Relationship Navigation**
   - Edge colors represent relationship strength:
     - 🔵 Strong confidence (>70%)
     - 🟠 Medium confidence (40-70%)
     - 🔴 Low confidence (<40%)
   - Click on a relationship to jump to the connected entity
   - Relationship edges show:
     - The predicate/mechanism (e.g., "supplies", "acquires")
     - Confidence level
     - Number of hops from the seed entity

### 4. **Search and Discovery**
   - Search box to find entities by name
   - Real-time highlighting of matching entities
   - Automatic highlighting of successor/predecessor nodes

### 5. **Graph Controls**
   - **Zoom In/Out**: Buttons in bottom-right to adjust zoom level
   - **Fit to Screen**: Center and fit the entire graph
   - **Reset View**: Reload the graph from scratch
   - **Show Stats**: Display overall graph statistics

### 6. **Statistics Panel**
   - Total nodes (entities) in the graph
   - Total edges (relationships) in the graph
   - Top hubs (most-connected entities)

## How It Works

### Data Sources

The page uses three API endpoints:

1. **`GET /finance/graph`** - Main graph data
   - Returns overall statistics and top entities
   - Optionally accepts `entity`, `hops`, `limit` parameters to get cascade from a specific entity

2. **`GET /finance/graph/{entity_name}/stories`** - Stories for an entity
   - Returns stories mentioning a specific entity
   - Efficient filtering at the API level

3. **`GET /finance/stories`** - Story listing (fallback)
   - Returns recent financial stories

### Data Flow

1. **Initialization**:
   - Page loads and fetches overall graph statistics
   - Top 40 entities are displayed as nodes
   - Graph is laid out using the COSE algorithm (physics-based)

2. **Entity Selection**:
   - User clicks on a node
   - Page fetches the cascade (knowledge graph walk) from that entity
   - Related stories are fetched and displayed
   - Sidebar updates with entity details and relationships

3. **Cascade Navigation**:
   - Graph links show how economic effects propagate through the network
   - Each link has an "order" (hops from seed): 1 = direct, 2 = one step removed, etc.
   - Confidence decays with distance (further connections are less certain)

## Backend Enhancement

A new endpoint has been added to support efficient entity story fetching:

```
GET /finance/graph/{entity_name}/stories
```

**Parameters:**
- `entity_name` (path): The entity name or canonical ID to search for
- `limit` (query): Maximum number of stories to return (default: 10, max: 50)

**Response:**
```json
{
  "entity": "HDFC Bank",
  "resolved_to": "HDFCBANK",
  "stories": [
    {
      "id": "story_id",
      "headline": "HDFC Bank reports earnings...",
      "narrative": "...",
      "event_type": "earnings",
      // ... other story fields
    }
  ],
  "count": 5
}
```

## Integration with Main App

### Option 1: Add Navigation Link
To add a link to the graph page in the main Descry app, add this to your navigation:

```html
<a href="/graph.html" class="nav-link">
  <span class="icon">📊</span>
  Knowledge Graph
</a>
```

### Option 2: Embed as Route
If you're using a framework-based approach, add a route:

```javascript
// In your router configuration
{
  path: '/graph',
  component: KnowledgeGraphPage,
  // Load graph.html or render the component
}
```

### Option 3: Open as Modal/Overlay
For navigation from specific stories or trends:

```javascript
// Open knowledge graph for a specific entity
window.location.href = '/graph.html?entity=HDFC+Bank';
```

The page will detect the `entity` query parameter and can be enhanced to support it.

## Customization

### Styling

The page uses CSS custom properties (variables) for theming. To customize colors, modify these in the `<style>` section:

```css
:root {
  --bg-primary: #ffffff;      /* Main background */
  --text-primary: #1a1a1a;    /* Main text */
  --accent: #0066cc;          /* Primary color */
  --success: #00b341;         /* Success color */
  --danger: #cc0000;          /* Danger/error color */
}
```

### Graph Layout

The visualization uses Cytoscape.js with the COSE layout algorithm. To adjust:

1. **Node Size**: Modify the `'width'` and `'height'` selectors in the Cytoscape style array
2. **Edge Color**: Update the `'line-color'` logic based on confidence thresholds
3. **Layout**: Change the `layout.name` from `'cose'` to alternatives:
   - `'breadthfirst'` - hierarchical
   - `'concentric'` - circular
   - `'grid'` - grid layout
   - `'circle'` - circular arrangement

### Adding Features

Some potential enhancements:

```javascript
// Example: Filter by entity type
function filterByType(type) {
  cy.elements().hide();
  cy.elements(`[type = "${type}"]`).show();
}

// Example: Highlight strongest paths
function highlightStrongestPaths(from, to) {
  const paths = cy.elements().aStar({
    roots: [cy.getElementById(from)],
    goal: cy.getElementById(to)
  }).path;
  paths.addClass('highlight');
}

// Example: Export graph as image
function exportAsImage() {
  const png = cy.png({ output: 'blob' });
  // Save as PNG
}
```

## Known Limitations

1. **Performance**: Large graphs (1000+ nodes) may render slowly. The page uses `max_links=40` by default to keep data manageable.

2. **Mobile**: The graph works on mobile but with limited screen space. Consider responsive adjustments for smaller screens.

3. **Entity Name Matching**: Entities are matched by canonical name (ticker symbol for companies). Variations may not match perfectly.

4. **Real-time Updates**: The graph is static and updates only when the page is refreshed. For live updates, consider adding polling or WebSocket support.

## API Reference

### GET /finance/graph

Returns knowledge graph statistics and top entities, or cascade around a specific entity.

**Query Parameters:**
- `entity` (optional): Entity name or ID to get cascade for
- `hops` (optional): Number of hops to traverse (1-4, default: 2)
- `limit` (optional): Maximum links to return (1-100, default: 40)

**Response (no entity):**
```json
{
  "stats": {
    "nodes": 234,
    "edges": 789,
    "hubs": [
      {"node": "Reserve Bank of India", "degree": 45},
      {"node": "HDFC Bank", "degree": 38}
    ]
  },
  "top": [
    {"id": "HDFCBANK", "name": "HDFC Bank", "type": "organization", "mentions": 12},
    // ... more entities
  ]
}
```

**Response (with entity):**
```json
{
  "entity": "HDFC Bank",
  "resolved_to": "HDFCBANK",
  "links": [
    {
      "from_entity": "HDFCBANK",
      "to_entity": "IndusInd Bank",
      "mechanism": "competes with",
      "order": 1,
      "confidence": 0.85,
      "direction": "unclear"
    },
    // ... more relationships
  ],
  "count": 8
}
```

### GET /finance/graph/{entity_name}/stories

Returns stories that mention a specific entity.

**Path Parameters:**
- `entity_name`: The entity name or canonical ID

**Query Parameters:**
- `limit` (optional): Maximum stories (1-50, default: 10)

**Response:**
```json
{
  "entity": "HDFC Bank",
  "resolved_to": "HDFCBANK",
  "stories": [
    {
      "id": "s123",
      "headline": "...",
      "narrative": "...",
      // ... story data
    }
  ],
  "count": 5
}
```

## Troubleshooting

### Graph Not Loading
- Check browser console for errors
- Verify `/finance/graph` API endpoint is accessible
- Ensure CORS is properly configured

### Nodes Not Clickable
- Try refreshing the page
- Check that JavaScript is enabled
- Verify Cytoscape.js CDN is loading (check Network tab)

### Sidebar Not Showing Details
- Ensure `/finance/graph/{entity}/stories` endpoint is working
- Check API response in Network tab
- Verify entity name is being sent correctly

### Poor Performance on Large Graphs
- Reduce `limit` parameter (default is 40)
- Decrease `max_hops` (default is 3)
- Try different layout algorithm (`breadthfirst` is faster than `cose`)

## Development Notes

### File Location
- Frontend: `/Users/prasadpandav/NewsLens-Backend/newslens/web/graph.html`
- Backend Enhancement: `/Users/prasadpandav/NewsLens-Backend/newslens/backend/app/main.py` (around line 2545)

### Dependencies
- **Cytoscape.js** (v3.28.1) - Graph visualization library
- **CDN**: Uses `cdnjs.cloudflare.com` for Cytoscape

### No Build Required
The page is a standalone HTML file with no build step. Changes take effect immediately (clear browser cache if needed).

## Future Enhancements

1. **Query Parameter Support**: `?entity=HDFC+Bank&hops=3`
2. **Trend Integration**: Show cascade from specific trends
3. **Story Timeline**: Show cascade evolution over time
4. **Export**: Save graph as PNG/SVG or as data
5. **Filters**: Filter by entity type, confidence threshold, time period
6. **Clustering**: Automatically group related entities
7. **Real-time Updates**: WebSocket for live graph changes
8. **Comparison**: Compare cascades from multiple entities side-by-side

## References

- Cytoscape.js: https://js.cytoscape.org/
- Knowledge Graph Implementation: `backend/app/finance/kg.py`
- Finance Schema: `backend/app/schemas/finance.py`
- Main API: `backend/app/main.py` (lines 2435-2577)
