# Quick Access Guide - Knowledge Graph

## Direct URLs

### Knowledge Graph Page
```
https://descry.in/graph.html
```

### Navigation Hub (shows all sections)
```
https://descry.in/nav-bridge.html
```

## Bookmark These Links

Add bookmarks to your browser for quick access:

### Browser Bookmark (Chrome/Firefox/Safari/Edge)
```
Name: Descry - Knowledge Graph
URL:  https://descry.in/graph.html
```

Or use this bookmarklet to open the graph while on any Descry page:
```javascript
javascript:window.open('/graph.html', '_self')
```

To add a bookmarklet:
1. Open your browser's bookmark manager
2. Create a new bookmark
3. Set the Name to "Open Graph"
4. Paste the bookmarklet code in the URL field
5. Save

## Browser Extensions (Optional)

### Create a Simple Extension

**manifest.json:**
```json
{
  "manifest_version": 3,
  "name": "Descry Knowledge Graph",
  "version": "1.0",
  "icons": {
    "16": "icon.png",
    "48": "icon.png",
    "128": "icon.png"
  },
  "action": {
    "default_title": "Open Knowledge Graph",
    "default_icon": "icon.png",
    "default_popup": "popup.html"
  },
  "permissions": ["activeTab", "scripting"],
  "host_permissions": [
    "https://descry.in/*"
  ]
}
```

**popup.html:**
```html
<!DOCTYPE html>
<html>
<head>
  <style>
    body { font-family: Arial; padding: 10px; width: 300px; }
    a { display: block; padding: 8px; margin: 4px 0; color: #0066cc; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h3>Descry</h3>
  <a href="https://descry.in/" target="_blank">Home</a>
  <a href="https://descry.in/trends" target="_blank">Trends</a>
  <a href="https://descry.in/graph.html" target="_blank">📊 Knowledge Graph</a>
  <a href="https://descry.in/saved" target="_blank">Saved</a>
</body>
</html>
```

## Adding to Main Navigation (Developer Guide)

### For React App

1. Find your navigation component (usually `Navigation.tsx` or `Nav.jsx`)
2. Add to your routes/menu:

```jsx
const navigationItems = [
  { path: '/', label: 'Stories', icon: '📰' },
  { path: '/trends', label: 'Trends', icon: '📈' },
  { path: '/graph.html', label: 'Knowledge Graph', icon: '🔗', external: true },
  { path: '/saved', label: 'Saved', icon: '🔖' },
  { path: '/profile', label: 'Profile', icon: '👤' },
];
```

3. Update the navigation rendering:

```jsx
{navigationItems.map(item => (
  item.external ? (
    <a key={item.path} href={item.path} className="nav-link">
      <span className="icon">{item.icon}</span>
      {item.label}
    </a>
  ) : (
    <Link key={item.path} to={item.path} className="nav-link">
      <span className="icon">{item.icon}</span>
      {item.label}
    </Link>
  )
))}
```

### For Vue App

1. Find your navigation configuration
2. Add to routes:

```js
{
  path: '/graph',
  meta: { 
    label: 'Knowledge Graph',
    icon: '🔗',
    external: true,
    href: '/graph.html'
  }
}
```

3. Update navigation template:

```vue
<template>
  <nav>
    <router-link 
      v-for="route in routes" 
      :key="route.path"
      v-if="!route.meta.external"
      :to="route.path"
      class="nav-link"
    >
      <span class="icon">{{ route.meta.icon }}</span>
      {{ route.meta.label }}
    </router-link>
    <a 
      v-for="route in routes" 
      :key="route.path"
      v-if="route.meta.external"
      :href="route.meta.href"
      class="nav-link"
    >
      <span class="icon">{{ route.meta.icon }}</span>
      {{ route.meta.label }}
    </a>
  </nav>
</template>
```

### For HTML/Vanilla JS App

Simply add an anchor tag to your navigation HTML:

```html
<nav>
  <a href="/" class="nav-link">
    <span class="icon">📰</span> Stories
  </a>
  <a href="/trends" class="nav-link">
    <span class="icon">📈</span> Trends
  </a>
  <a href="/graph.html" class="nav-link">
    <span class="icon">🔗</span> Knowledge Graph
  </a>
  <a href="/saved" class="nav-link">
    <span class="icon">🔖</span> Saved
  </a>
</nav>
```

## Features in Knowledge Graph Page

Once you access the graph page, you'll have:

- **Interactive Visualization**: Click on entities to explore connections
- **Search**: Find specific companies or entities
- **Related Stories**: See which stories mention each entity
- **Relationships**: View how entities are connected
- **Statistics**: Overall graph metrics and top hubs
- **Navigation**: Quick links back to home, trends, and stories

## Keyboard Shortcuts (Future Enhancement)

We can add keyboard shortcuts later:
- `G` - Go to Knowledge Graph
- `Home` - Return to home
- `T` - Go to Trends
- `S` - Go to Saved stories

## Troubleshooting

### Graph Page Not Loading
1. Check browser console (F12) for errors
2. Ensure `/finance/graph` API is accessible
3. Clear browser cache and refresh
4. Check CORS configuration if on different domain

### Links Not Working
1. Verify URLs are correct
2. Check if `/graph.html` file exists on server
3. Verify DNS/domain configuration
4. Test from different network

### Performance Issues
- Graph loads large datasets - may take 1-2 seconds
- On slow connections, wait for "Loading..." to complete
- Try refreshing if graph seems incomplete

## Monitoring Access

Check your analytics to see how many users are accessing the knowledge graph:
- Page: `/graph.html`
- Query params: `?entity=` for specific entity views

## Next Steps

1. **Immediate**: Use the direct URLs or nav-bridge
2. **Short-term**: Add link to your main navigation
3. **Long-term**: Consider mobile-optimized version or 3D visualization

## Support

For issues or feature requests:
- Check `/GRAPH_PAGE.md` for detailed documentation
- Review `/KNOWLEDGE_GRAPH_IMPLEMENTATION.md` for technical details
- Check browser console for error messages
