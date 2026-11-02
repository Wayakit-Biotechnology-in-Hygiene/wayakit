{
    "name": "WhatsApp Channel Prefix",
    "summary": "Puts a per-account \"[PREFIX] \" in front of WhatsApp channel names in "
               "Discuss, so KSA and Mexico conversations can be told apart.",
    "category": "Productivity/Discuss",
    "version": "17.0.1.0.0",
    "author": "Wayakit",
    "website": "https://wayakit.com",
    "license": "LGPL-3",
    "depends": ["whatsapp"],
    "data": [
        "views/whatsapp_account_views.xml",
        "data/server_action.xml",
    ],
    "application": False,
    "installable": True,
}
