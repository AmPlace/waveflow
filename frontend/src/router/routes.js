export const appRoutes = [
  {
    path: '/radio',
    name: 'radio',
    component: () => import('../views/Home.vue'),
  },
  {
    path: '/tv',
    name: 'tv',
    component: () => import('../views/IptvHome.vue'),
  },
  {
    path: '/',
    redirect: '/radio',
    meta: { compatibilityRoute: true },
  },
  {
    path: '/iptv',
    redirect: '/tv',
    meta: { compatibilityRoute: true },
  },
  {
    path: '/settings',
    component: () => import('../views/settings/SettingsView.vue'),
    redirect: '/settings/sources',
    meta: { requiresAdmin: true },
    children: [
      {
        path: 'sources',
        name: 'settings-sources',
        component: () => import('../views/settings/LiveSourcesSettings.vue'),
      },
      {
        path: 'epg',
        component: () => import('../views/settings/EpgSettingsView.vue'),
        redirect: '/settings/epg/sources',
        children: [
          {
            path: 'sources',
            name: 'settings-epg-sources',
            component: () => import('../views/settings/EpgSourcesSettings.vue'),
          },
          {
            path: 'matching',
            name: 'settings-epg-matching',
            component: () => import('../views/settings/EpgMatchingSettings.vue'),
          },
          {
            path: ':epgPath(.*)*',
            redirect: '/settings/epg/sources',
          },
        ],
      },
      {
        path: 'plugins',
        name: 'settings-plugins',
        component: () => import('../views/settings/PluginsSettings.vue'),
      },
      {
        path: 'security',
        name: 'settings-security',
        component: () => import('../views/settings/SecuritySettings.vue'),
      },
      {
        path: ':settingsPath(.*)*',
        redirect: '/settings/sources',
      },
    ],
  },
  {
    path: '/admin',
    redirect: '/settings/sources',
    meta: { requiresAdmin: true, compatibilityRoute: true },
  },
  {
    path: '/market',
    name: 'market',
    component: () => import('../views/MarketView.vue'),
    meta: { requiresAdmin: true },
  },
  {
    path: '/admin/epg',
    name: 'legacy-epg-redirect',
    redirect: '/settings/epg/matching',
    meta: { requiresAdmin: true, compatibilityRoute: true },
  },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('../views/SetupView.vue'),
    meta: { authPage: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { authPage: true },
  },
]
