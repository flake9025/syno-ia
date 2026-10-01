/* Traductions de l'interface (français par défaut, anglais en secours). */
const I18N = {
  fr: {
    'login.subtitle': 'Interrogez vos documents du NAS',
    'login.account': 'Compte DSM',
    'login.password': 'Mot de passe',
    'login.otp': 'Code de vérification (2FA)',
    'login.submit': 'Se connecter',
    'login.pending': 'Connexion…',
    'login.hint': "Vos identifiants sont vérifiés par DSM. syno-ia ne voit que les documents auxquels votre compte a accès.",
    'login.otpRequired': 'Saisissez le code de vérification en deux étapes.',
    'login.failed': 'Connexion impossible.',
    'app.title': 'Assistant documentaire',
    'app.newChat': 'Nouvelle conversation',
    'app.shares': 'Vos dossiers',
    'app.documents': 'Documents indexés',
    'app.filter': 'Filtrer…',
    'app.placeholder': 'Posez votre question…',
    'app.logout': 'Se déconnecter',
    'app.noShares': 'Aucun dossier partagé accessible.',
    'app.noDocs': 'Aucun document indexé pour vous.',
    'app.sources': 'Sources',
    'app.thinking': 'Recherche dans vos documents…',
    'app.filtered': '{n} extrait(s) écarté(s) faute de droits.',
    'app.lexicalOnly': 'Recherche par mots-clés uniquement (embeddings désactivés).',
    'app.copy': 'Copier',
    'app.copied': 'Copié',
    'app.open': 'Télécharger',
    'app.stopped': 'Génération interrompue.',
    'welcome.title': 'Que cherchez-vous ?',
    'welcome.body': "Posez une question en langage naturel. Les réponses sont construites uniquement à partir des documents auxquels votre compte DSM a accès.",
    'suggestion.1': 'Résume les documents récents',
    'suggestion.2': 'Quelles sont les procédures de sauvegarde ?',
    'suggestion.3': 'Retrouve la facture du mois dernier',
    'admin.title': 'Administration',
    'admin.overview': "Vue d'ensemble",
    'admin.index': 'Indexation',
    'admin.models': 'Modèles',
    'admin.indexStart': "Lancer l'indexation",
    'admin.indexFull': 'Réindexer tout',
    'admin.indexCancel': 'Annuler',
    'admin.indexOptimize': 'Optimiser',
    'admin.download': 'Télécharger',
    'admin.installed': 'Installé',
    'admin.recommended': 'Recommandé',
    'admin.hardware': 'Matériel détecté',
    'admin.engine': 'Moteurs',
    'admin.indexState': 'Index',
    'admin.dsm': 'Connexion DSM',
    'error.session': 'Session expirée, reconnexion nécessaire.',
    'error.network': 'Le serveur est injoignable.',
  },
  en: {
    'login.subtitle': 'Query your NAS documents',
    'login.account': 'DSM account',
    'login.password': 'Password',
    'login.otp': 'Verification code (2FA)',
    'login.submit': 'Sign in',
    'login.pending': 'Signing in…',
    'login.hint': 'Your credentials are verified by DSM. syno-ia only sees documents your account can access.',
    'login.otpRequired': 'Enter your two-factor verification code.',
    'login.failed': 'Sign-in failed.',
    'app.title': 'Document assistant',
    'app.newChat': 'New conversation',
    'app.shares': 'Your folders',
    'app.documents': 'Indexed documents',
    'app.filter': 'Filter…',
    'app.placeholder': 'Ask your question…',
    'app.logout': 'Sign out',
    'app.noShares': 'No accessible shared folder.',
    'app.noDocs': 'No document indexed for you.',
    'app.sources': 'Sources',
    'app.thinking': 'Searching your documents…',
    'app.filtered': '{n} excerpt(s) hidden by permissions.',
    'app.lexicalOnly': 'Keyword search only (embeddings disabled).',
    'app.copy': 'Copy',
    'app.copied': 'Copied',
    'app.open': 'Download',
    'app.stopped': 'Generation stopped.',
    'welcome.title': 'What are you looking for?',
    'welcome.body': 'Ask a question in plain language. Answers are built only from documents your DSM account can access.',
    'suggestion.1': 'Summarise the recent documents',
    'suggestion.2': 'What are the backup procedures?',
    'suggestion.3': 'Find last month invoice',
    'admin.title': 'Administration',
    'admin.overview': 'Overview',
    'admin.index': 'Indexing',
    'admin.models': 'Models',
    'admin.indexStart': 'Start indexing',
    'admin.indexFull': 'Full reindex',
    'admin.indexCancel': 'Cancel',
    'admin.indexOptimize': 'Optimise',
    'admin.download': 'Download',
    'admin.installed': 'Installed',
    'admin.recommended': 'Recommended',
    'admin.hardware': 'Detected hardware',
    'admin.engine': 'Engines',
    'admin.indexState': 'Index',
    'admin.dsm': 'DSM connection',
    'error.session': 'Session expired, please sign in again.',
    'error.network': 'Server unreachable.',
  },
};

let LANG = (localStorage.getItem('syno-ia.lang') || navigator.language || 'fr').slice(0, 2);
if (!I18N[LANG]) LANG = 'fr';

function t(key, vars) {
  let value = (I18N[LANG] && I18N[LANG][key]) || I18N.fr[key] || key;
  if (vars) {
    for (const [name, replacement] of Object.entries(vars)) {
      value = value.replace(`{${name}}`, replacement);
    }
  }
  return value;
}

function setLang(lang) {
  LANG = I18N[lang] ? lang : 'fr';
  localStorage.setItem('syno-ia.lang', LANG);
  document.documentElement.lang = LANG;
  applyTranslations();
}

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
}
